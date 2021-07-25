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
This module contains functions for computing the finite-difference gradient
of a quantum tape.
"""
# pylint: disable=protected-access
import functools

import numpy as np
from scipy.special import factorial

import pennylane as qml


def get_stencil(n, order, form):
    if n < 1:
        raise ValueError("Derivative order n must be a positive integer.")

    num_points = order + 2 * np.floor((n + 1) / 2) - 1
    N = num_points + 1 if n % 2 == 0 else num_points

    if form == "forward":
        shifts = np.arange(N, dtype=np.float64)

    elif form == "backward":
        shifts = np.arange(-N + 1, 1, dtype=np.float64)

    elif form == "center":
        if order % 2 != 0:
            raise ValueError("Centered finite-difference requires an even order.")

        N = num_points // 2
        shifts = np.arange(-N, N + 1, dtype=np.float64)

    else:
        raise ValueError(f"Unknown form {form}. Must be one of 'forward', 'backward', 'center'.")

    A = shifts ** np.arange(len(shifts)).reshape(-1, 1)
    b = np.zeros_like(shifts)
    b[n] = factorial(n)
    coeffs = np.linalg.solve(A, b)

    stencil = np.stack([coeffs, shifts])
    stencil[0, np.abs(stencil[0, :]) < 1e-10] = 0
    stencil = stencil[:, ~np.all(stencil == 0, axis=0)]
    stencil = stencil[:, np.argsort(np.abs(stencil)[1])]
    return stencil


def get_shifted_tapes(tape, idx, shifts, h=1e-7):
    r"""Generate the first-order forward finite-difference tapes and postprocessing
    methods required to compute the gradient of a gate parameter.

    Args:
        tape (.QuantumTape): quantum tape to differentiate
        idx (int): trainable parameter index to differentiate with respect to
        h=1e-7 (float): finite difference method step size

    Returns:
        tuple[list[QuantumTape], function]: A tuple containing the list of generated tapes,
        in addition to a post-processing function to be applied to the evaluated
        tapes.
    """
    params = qml.math.stack(tape.get_parameters())
    tapes = []

    for s in shifts:
        shifted_tape = tape.copy(copy_operations=True)

        shift = np.zeros(qml.math.shape(params), dtype=np.float64)
        shift[idx] = s * h

        shifted_params = params + qml.math.convert_like(shift, params)
        shifted_tape.set_parameters(qml.math.unstack(shifted_params))

        tapes.append(shifted_tape)

    return tapes


def finite_diff(tape, argnum=None, h=1e-7, order=1, n=1, form="forward"):
    r"""Generate the parameter-shift tapes and postprocessing methods required
    to compute the gradient of an gate parameter with respect to an
    expectation value.

    Args:
        tape (.QuantumTape): quantum tape to differentiate
        argnum (int or list[int] or None): Trainable parameter indices to differentiate
            with respect to. If not provided, the derivative with respect to all
            trainable indices are returned.
        h (float): finite difference method step size
        order (int): The order of the finite difference method to use.
        n (int): compute the :math:`n`-th derivative
        form (str): The form of the finite difference method. Must be one of
            ``"forward"``, ``"center"``, or ``"backward"``.

    Returns:
        tuple[list[QuantumTape], function]: A tuple containing a
        list of generated tapes, in addition to a post-processing
        function to be applied to the evaluated tapes.

    **Example**

    >>> with qml.tape.QuantumTape() as tape:
    ...     qml.RX(params[0], wires=0)
    ...     qml.RY(params[1], wires=0)
    ...     qml.RX(params[2], wires=0)
    ...     qml.expval(qml.PauliZ(0))
    ...     qml.var(qml.PauliZ(0))
    >>> tape.trainable_params = {0, 1, 2}
    >>> gradient_tapes, fn = gradients.finite_difference.grad(tape)
    >>> res = dev.batch_execute(gradient_tapes)
    >>> fn(res)
    [[-0.38751721 -0.18884787 -0.38355704]
     [ 0.69916862  0.34072424  0.69202359]]
    """
    # TODO: replace the JacobianTape._grad_method_validation
    # functionality before deprecation.
    diff_methods = tape._grad_method_validation("numeric")

    if not tape.trainable_params or all(g == "0" for g in diff_methods):
        # Either all parameters have grad method 0, or there are no trainable
        # parameters.
        return [[]], []

    gradient_tapes = []
    shapes = []
    c0 = None

    coeffs, shifts = get_stencil(n, order, form)

    if 0 in shifts:
        c0 = coeffs[0]
        gradient_tapes.append(tape)
        shifts = shifts[1:]
        coeffs = coeffs[1:]

    # TODO: replace the JacobianTape._choose_params_with_methods
    # functionality before deprecation.
    for idx, (t_idx, dm) in enumerate(tape._choose_params_with_methods(diff_methods, argnum)):
        if dm == "0":
            shapes.append(0)
            continue

        g_tapes = get_shifted_tapes(tape, t_idx, shifts, h=h)
        gradient_tapes.extend(g_tapes)
        shapes.append(len(g_tapes))

    def processing_fn(results):
        grads = []
        start = 1 if c0 is not None else 0

        for s in shapes:

            if s == 0:
                g = qml.math.convert_like(np.zeros([tape.output_dim]), results)
                grads.append(g)
                continue

            res = results[start : start + s]
            start = start + s

            res = qml.math.stack(res)
            g = sum([c * r for c, r in zip(coeffs, res)])

            if c0 is not None:
                g = g + c0 * results[0]

            grads.append(qml.math.squeeze(g / (h ** n)))

            # g = [0] * len(res[0])

            # for c, r in zip(coeffs, res):
            #     for idx, i in enumerate(r):
            #         g[idx] += c * np.array(i)

            # if c0 is not None:
            #     g = [i + c0 * r for i, r in zip(g, results[0])]

            # g = [i / (h ** n) for i in g]
            # grads.append(g)

        return qml.math.stack(grads).T

    return gradient_tapes, processing_fn
