from typing import Any, Dict, Optional, Tuple

import jax.numpy as jnp
import numpy as np

import jaxsim.typing as jtp
from jaxsim.physics import algos
from jaxsim.physics.algos.soft_contacts import (
    SoftContactsParams,
    collidable_points_pos_vel,
    soft_contacts_model,
)
from jaxsim.physics.algos.terrain import FlatTerrain, Terrain
from jaxsim.physics.model.physics_model import PhysicsModel

from . import ode_data


def compute_contact_forces(
    physics_model: PhysicsModel,
    ode_state: ode_data.ODEState,
    soft_contacts_params: SoftContactsParams = SoftContactsParams(),
    terrain: Terrain = FlatTerrain(),
) -> Tuple[jtp.Matrix, jtp.Matrix, jtp.Matrix]:

    # Compute position and linear velocity (inertial representation)
    # of all model's collidable points
    pos_cp, vel_cp = collidable_points_pos_vel(
        model=physics_model,
        q=ode_state.physics_model.joint_positions,
        qd=ode_state.physics_model.joint_velocities,
        xfb=ode_state.physics_model.xfb(),
    )

    # Compute the forces acting on the collidable points due to contact with
    # the compliant ground surface
    contact_forces_points, tangential_deformation_dot, _ = soft_contacts_model(
        positions=pos_cp,
        velocities=vel_cp,
        tangential_deformation=ode_state.soft_contacts.tangential_deformation,
        soft_contacts_params=soft_contacts_params,
        terrain=terrain,
    )

    # Initialize the contact forces, one per body
    contact_forces_links = jnp.zeros_like(
        ode_data.ODEInput.zero(physics_model).physics_model.f_ext
    )

    # Combine the contact forces of all collidable points belonging to the same body
    for body_idx in set(physics_model.gc.body):

        body_idx = int(body_idx)
        contact_forces_links = contact_forces_links.at[body_idx, :].set(
            jnp.sum(contact_forces_points[:, physics_model.gc.body == body_idx], axis=1)
        )

    return contact_forces_links, tangential_deformation_dot, contact_forces_points.T


def dx_dt(
    x: ode_data.ODEState,
    t: Optional[jtp.Float],
    physics_model: PhysicsModel,
    soft_contacts_params: SoftContactsParams = SoftContactsParams(),
    ode_input: ode_data.ODEInput = None,
    terrain: Terrain = FlatTerrain(),
) -> Tuple[ode_data.ODEState, Dict[str, Any]]:

    if t is not None and isinstance(t, np.ndarray) and t.size != 1:
        raise ValueError(t.size)

    # Initialize arguments
    ode_state = x
    ode_input = (
        ode_input
        if ode_input is not None
        else ode_data.ODEInput.zero(physics_model=physics_model)
    )

    # ======================
    # Compute contact forces
    # ======================

    # Initialize the collidable points contact forces
    contact_forces_points = None

    # Initialize the contact forces, one per body
    contact_forces_links = jnp.zeros_like(ode_input.physics_model.f_ext)

    # Initialize the derivative of the tangential deformation
    tangential_deformation_dot = jnp.zeros_like(
        ode_state.soft_contacts.tangential_deformation
    )

    if physics_model.gc.body.size > 0:
        (
            contact_forces_links,
            tangential_deformation_dot,
            contact_forces_points,
        ) = compute_contact_forces(
            physics_model=physics_model,
            soft_contacts_params=soft_contacts_params,
            ode_state=ode_state,
            terrain=terrain,
        )

    # ==============
    # Joint friction
    # ==============

    # Static and viscous joint friction parameters
    kc = jnp.array(list(physics_model._joint_friction_static.values()))
    kv = jnp.array(list(physics_model._joint_friction_viscous.values()))

    # Compute the joint friction torque
    tau_friction = -(
        jnp.diag(kc) @ jnp.sign(ode_state.physics_model.joint_positions)
        + jnp.diag(kv) @ ode_state.physics_model.joint_velocities
    )

    # ========================
    # Compute forward dynamics
    # ========================

    # Compute the total forces applied to the bodies
    total_forces = ode_input.physics_model.f_ext + contact_forces_links

    # Compute the joint torques to actuate
    tau = ode_input.physics_model.tau + tau_friction

    W_a_WB, qdd = algos.aba.aba(
        model=physics_model,
        xfb=ode_state.physics_model.xfb(),
        q=ode_state.physics_model.joint_positions,
        qd=ode_state.physics_model.joint_velocities,
        tau=tau,
        f_ext=total_forces,
    )

    # =========================================
    # Compute the state derivative of base link
    # =========================================

    if not physics_model.is_floating_base:

        W_Qd_B = jnp.zeros(shape=[4, 1])
        BW_v_WB = jnp.zeros(shape=[3, 1])

    else:

        from jaxsim.math.conv import Convert
        from jaxsim.math.quaternion import Quaternion

        W_Qd_B = Quaternion.derivative(
            quaternion=ode_state.physics_model.base_quaternion,
            omega=ode_state.physics_model.base_angular_velocity,
            omega_in_body_fixed=False,
        ).squeeze()

        # Compute linear component of mixed velocity
        BW_v_WB = Convert.velocities_threed(
            v_6d=jnp.hstack(
                [
                    ode_state.physics_model.base_linear_velocity,
                    ode_state.physics_model.base_angular_velocity,
                ]
            ),
            p=ode_state.physics_model.base_position,
        ).squeeze()

    # Derivative of xfb (floating-base state)
    xd_fb = jnp.hstack([W_Qd_B, BW_v_WB, W_a_WB.squeeze()]).squeeze()

    # =====================================
    # Build the full derivative of ODEState
    # =====================================

    def fix_one_dof(vector: jtp.Vector) -> Optional[jtp.Vector]:

        if vector is None:
            return None

        return jnp.array([vector]) if vector.shape == () else vector

    physics_model_state_derivative = ode_state.physics_model.replace(
        joint_positions=fix_one_dof(ode_state.physics_model.joint_velocities.squeeze()),
        joint_velocities=fix_one_dof(qdd.squeeze()),
        base_quaternion=xd_fb.squeeze()[0:4],
        base_position=xd_fb.squeeze()[4:7],
        base_angular_velocity=xd_fb.squeeze()[10:13],
        base_linear_velocity=xd_fb.squeeze()[7:10],
    )

    soft_contacts_state_derivative = ode_state.soft_contacts.replace(
        tangential_deformation=tangential_deformation_dot.squeeze(),
    )

    # We store the state derivative using the ODEState class so that the pytree
    # structure remains consistent, and it allows using our generic pytree integrators
    state_derivative = ode_data.ODEState(
        physics_model=physics_model_state_derivative,
        soft_contacts=soft_contacts_state_derivative,
    )

    # ===============================
    # Build auxiliary data and return
    # ===============================

    # Real ODEInput containing the real joint forces that have been actuated and
    # the total external forces (= original external forces + terrain + limits)
    ode_input_real = ode_data.ODEInput(
        physics_model=ode_data.PhysicsModelInput(tau=tau, f_ext=total_forces)
    )

    # Pack the inertial-fixed floating-base acceleration
    W_nud_WB = jnp.hstack([W_a_WB.squeeze(), qdd.squeeze()])

    # Build the auxiliary data
    aux_dict = dict(
        model_acceleration=W_nud_WB,
        ode_input=ode_input,
        ode_input_real=ode_input_real,
        contact_forces_points=contact_forces_points,
        tangential_deformation_dot=tangential_deformation_dot,
    )

    # Return the state derivative as a generic PyTree, and the dict with auxiliary info
    return state_derivative, aux_dict
