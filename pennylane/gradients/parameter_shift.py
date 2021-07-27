# Copyright 2018-2021 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
This module contains functions for computing the parameter-shift gradient
of a qubit-based quantum tape.
"""
# pylint: disable=protected-access
import numpy as np

import pennylane as qml

from .finite_difference import finite_diff, generate_shifted_tapes


def _get_operation_recipe(tape, t_idx, shift=np.pi / 2):
    p_idx = list(tape.trainable_params)[t_idx]
    op = tape._par_info[p_idx]["op"]
    op_p_idx = tape._par_info[p_idx]["p_idx"]
    return op.get_parameter_shift(op_p_idx, shift=shift)


def _process_gradient_recipe(gr):
    stencil = np.array(gr).T
    stencil[0, np.abs(stencil[0, :]) < 1e-10] = 0
    stencil = stencil[:, ~np.all(stencil == 0, axis=0)]
    return stencil[:, np.argsort(np.abs(stencil)[-1])]


def _gradient_analysis(tape):
    """Update the parameter information dictionary with gradient information
    of each parameter"""
    tape._gradient_fn = param_shift

    for idx, info in tape._par_info.items():

        if idx not in tape.trainable_params:
            info["grad_method"] = None
        else:
            op = tape._par_info[idx]["op"]

            if op.grad_method == "F":
                info["grad_method"] = "F"
            else:
                info["grad_method"] = tape._grad_method(idx, use_graph=True, default_method="A")


def _expval_parameter_shift(tape, shift, gradient_recipes, method_map):

    gradient_tapes = []
    gradient_coeffs = []
    shapes = []
    c0 = []

    for gr, t_idx in zip(gradient_recipes, tape.trainable_params):
        if t_idx not in method_map or method_map[t_idx] == "0":
            shapes.append(0)
            gradient_coeffs.append([])
            continue

        if gr is None:
            gr = _get_operation_recipe(tape, t_idx, shift=shift)

        coeffs, multipliers, shifts = _process_gradient_recipe(gr)

        if 0 in shifts:
            if not c0:
                gradient_tapes.append(tape)

            c0.append(coeffs[0])
            coeffs = coeffs[1:]
            multipliers = multipliers[1:]
            shifts = shifts[1:]

        g_tapes = generate_shifted_tapes(tape, t_idx, shifts, multipliers)

        gradient_tapes.extend(g_tapes)
        gradient_coeffs.append(coeffs)
        shapes.append(len(g_tapes))

    def processing_fn(results):
        grads = []
        start = 1 if c0 else 0

        for i, s in enumerate(shapes):

            if s == 0:
                g = qml.math.convert_like(np.zeros([tape.output_dim]), results)
                grads.append(g)
                continue

            res = results[start : start + s]
            start = start + s

            res = qml.math.stack(res)
            g = sum([c * r for c, r in zip(gradient_coeffs[i], res)])

            if c0:
                g = g + c0[i] * results[0]

            grads.append(g)

        # The following is for backwards compatibility; currently,
        # the device stacks multiple measurement arrays, even if not the same
        # size, resulting in a ragged array.
        # In the future, we might want to change this so that only tuples
        # of arrays are returned.
        for i, g in enumerate(grads):
            g = qml.math.convert_like(g, res[0])
            if hasattr(g, "dtype") and g.dtype is np.dtype("object"):
                grads[i] = qml.math.hstack(g)

        return qml.math.T(qml.math.stack(grads))

    return gradient_tapes, processing_fn


def _generate_variance_data(tape, var_idx):
    """Given an input tape with terminal variance measurements,
    return a copy of the tape with only terminal expectation values.
    In addition, if there are non-involutary observables measured,
    a second tape is returned with the observable replaced with its square.
    """
    # Get <A>, the expectation value of the tape with unshifted parameters.
    expval_tape = tape.copy(copy_operations=True)
    expval_sq_tape = None

    # Convert all variance measurements on the tape into expectation values
    for i in var_idx:
        obs = expval_tape._measurements[i].obs
        expval_tape._measurements[i] = qml.measure.MeasurementProcess(
            qml.operation.Expectation, obs=obs
        )

    # For involutory observables (A^2 = I) we have d<A^2>/dp = 0.
    # Currently, the only observable we have in PL that may be non-involutory is qml.Hermitian
    involutory = [i for i in var_idx if tape.observables[i].name != "Hermitian"]

    # If there are non-involutory observables A present, we must compute d<A^2>/dp.
    non_involutory = set(var_idx) - set(involutory)

    if non_involutory:
        expval_sq_tape = tape.copy(copy_operations=True)

        for i in non_involutory:
            # We need to calculate d<A^2>/dp; to do so, we replace the
            # involutory observables A in the queue with A^2.
            obs = expval_sq_tape._measurements[i].obs
            A = obs.matrix

            obs = qml.Hermitian(A @ A, wires=obs.wires)
            expval_sq_tape._measurements[i] = qml.measure.MeasurementProcess(
                qml.operation.Expectation, obs=obs
            )

    return expval_tape, expval_sq_tape, involutory


def _var_parameter_shift(tape, shift, gradient_recipes, method_map):

    var_mask = [m.return_type is qml.operation.Variance for m in tape.measurements]

    # Store the locations of any variance measurements
    # in the measurement queue.
    var_idx = np.where(var_mask)[0]

    gradient_tapes = []

    expval_tape, expval_sq_tape, involutory = _generate_variance_data(tape, var_idx)
    gradient_tapes.append(expval_tape)

    pdA_tapes, pdA_fn = _expval_parameter_shift(expval_tape, shift, gradient_recipes, method_map)
    gradient_tapes.extend(pdA_tapes)
    tape_boundary = len(pdA_tapes) + 1

    if expval_sq_tape is not None:
        # Non-involutory observables are present; the partial derivative of <A^2>
        # may be non-zero. Here, we calculate the analytic derivatives of the <A^2>
        # observables.
        pdA2_tapes, pdA2_fn = _expval_parameter_shift(
            expval_sq_tape, shift, gradient_recipes, method_map
        )
        gradient_tapes.extend(pdA2_tapes)

    def processing_fn(results):
        f0 = qml.math.reshape(results[0], [-1, 1])

        pdA = pdA_fn(results[1 : tape_boundary])
        pdA2 = 0

        if expval_sq_tape is not None:
            pdA2 = pdA2_fn(results[tape_boundary :])

            if involutory:
                qml.math.where(qml.math.reshape(involutory, [-1, 1]), 0, pdA2)

        return qml.math.where(qml.math.reshape(var_mask, [-1, 1]), pdA2 - 2 * f0 * pdA, pdA)

    return gradient_tapes, processing_fn


def param_shift(tape, argnum=None, shift=np.pi / 2, gradient_recipes=None, fallback_fn=finite_diff):
    r"""Generate the parameter-shift tapes and postprocessing methods required
    to compute the gradient of an gate parameter with respect to an
    expectation value.

    Args:
        tape (.QuantumTape): quantum tape to differentiate
        argnum (int or list[int] or None): Trainable parameter indices to differentiate
            with respect to. If not provided, the derivative with respect to all
            trainable indices are returned.
        shift (float): The shift value to use for the two-term parameter-shift formula.
            Only valid if the operation in question supports the two-term parameter-shift
            rule (that is, it has two distinct eigenvalues) and ``gradient_recipe``
            is ``None``.
        gradient_recipes (tuple(list[list[float]] or None)): List of gradient recipes
            for the parameter-shift method. One gradient recipe must be provided
            per trainable parameter.

            This is a tuple with one nested list per parameter. For
            parameter :math:`\phi_k`, the nested list contains elements of the form
            :math:`[c_i, a_i, s_i]` where :math:`i` is the index of the
            term, resulting in a gradient recipe of

            .. math:: \frac{\partial}{\partial\phi_k}f = \sum_{i} c_i f(a_i \phi_k + s_i).

            If ``None``, the default gradient recipe containing the two terms
            :math:`[c_0, a_0, s_0]=[1/2, 1, \pi/2]` and :math:`[c_1, a_1,
            s_1]=[-1/2, 1, -\pi/2]` is assumed for every parameter.
        fallback_fn (None or Callable): a fallback grdient function to use for
            any parameters that do not support the parameter-shift rule.

    Returns:
        tuple[list[QuantumTape], function]: A tuple containing a
        list of generated tapes, in addition to a post-processing
        function to be applied to the evaluated tapes.

    For a variational evolution :math:`U(\mathbf{p}) \vert 0\rangle` with
    :math:`N` parameters :math:`\mathbf{p}`,
    consider the expectation value of an observable :math:`O`:

    .. math::

        f(\mathbf{p})  = \langle \hat{O} \rangle(\mathbf{p}) = \langle 0 \vert
        U(\mathbf{p})^\dagger \hat{O} U(\mathbf{p}) \vert 0\rangle.


    The gradient of this expectation value can be calculated using :math:`2N` expectation
    values using the parameter-shift rule:

    .. math::

        \frac{\partial f}{\partial \mathbf{p}} = \frac{1}{2\sin s} \left[ f(\mathbf{p} + s) -
        f(\mathbf{p} -s) \right].

    **Gradients of variances**

    For a variational evolution :math:`U(\mathbf{p}) \vert 0\rangle` with
    :math:`N` parameters :math:`\mathbf{p}`,
    consider the variance of an observable :math:`O`:

    .. math::

        g(\mathbf{p})=\langle \hat{O}^2 \rangle (\mathbf{p}) - [\langle \hat{O}
        \rangle(\mathbf{p})]^2.

    We can relate this directly to the parameter-shift rule by noting that

    .. math::

        \frac{\partial g}{\partial \mathbf{p}}= \frac{\partial}{\partial
        \mathbf{p}} \langle \hat{O}^2 \rangle (\mathbf{p})
        - 2 f(\mathbf{p}) \frac{\partial f}{\partial \mathbf{p}}.

    This results in :math:`4N + 1` evaluations.

    In the case where :math:`O` is involutory (:math:`\hat{O}^2 = I`), the first term in the above
    expression vanishes, and we are simply left with

    .. math::

      \frac{\partial g}{\partial \mathbf{p}} = - 2 f(\mathbf{p})
      \frac{\partial f}{\partial \mathbf{p}},

    allowing us to compute the gradient using :math:`2N + 1` evaluations.

    **Example**

    >>> with qml.tape.QuantumTape() as tape:
    ...     qml.RX(params[0], wires=0)
    ...     qml.RY(params[1], wires=0)
    ...     qml.RX(params[2], wires=0)
    ...     qml.expval(qml.PauliZ(0))
    ...     qml.var(qml.PauliZ(0))
    >>> tape.trainable_params = {0, 1, 2}
    >>> gradient_tapes, fn = qml.gradients.param_shift(tape)
    >>> res = dev.batch_execute(gradient_tapes)
    >>> fn(res)
    [[-0.38751721 -0.18884787 -0.38355704]
     [ 0.69916862  0.34072424  0.69202359]]
    """

    # =================================================================
    # Validation

    if any(m.return_type is qml.operation.State for m in tape.measurements):
        raise ValueError("Does not support circuits that return the state")

    # perform gradient method validation
    if getattr(tape, "_gradient_fn", None) != param_shift:
        _gradient_analysis(tape)

    # TODO: replace the JacobianTape._grad_method_validation
    # functionality before deprecation.
    method = "analytic" if fallback_fn is None else "best"
    diff_methods = tape._grad_method_validation(method)

    if not tape.trainable_params or all(g == "0" for g in diff_methods):
        # Either all parameters have grad method 0, or there are no trainable
        # parameters.
        return [], lambda x: np.zeros([tape.output_dim, len(tape.trainable_params)])

    # TODO: replace the JacobianTape._choose_params_with_methods
    # functionality before deprecation.
    method_map = dict(tape._choose_params_with_methods(diff_methods, argnum))
    gradient_tapes = []

    # =================================================================
    # Fallback functionality

    unsupported_params = {idx for idx, g in method_map.items() if g == "F"}

    if unsupported_params:
        g_tapes, fallback_proc_fn = fallback_fn(tape, argnum=unsupported_params)
        gradient_tapes.extend(g_tapes)
        fallback_len = len(g_tapes)

        method_map = {t_idx: dm for t_idx, dm in method_map.items() if dm != "F"}

    # =================================================================
    # Check for variances
    var_mask = [m.return_type is qml.operation.Variance for m in tape.measurements]

    # =================================================================
    # Generate parameter-shift gradient tapes

    if gradient_recipes is None:
        gradient_recipes = [None] * len(tape.trainable_params)

    if any(var_mask):
        g_tapes, fn = _var_parameter_shift(tape, shift, gradient_recipes, method_map)
    else:
        g_tapes, fn = _expval_parameter_shift(tape, shift, gradient_recipes, method_map)

    gradient_tapes.extend(g_tapes)

    if unsupported_params:

        def processing_fn(results):
            unsupported_grads = fallback_proc_fn(results[:fallback_len])
            supported_grads = fn(results[fallback_len:])
            return unsupported_grads + supported_grads

    else:
        processing_fn = fn

    return gradient_tapes, processing_fn
