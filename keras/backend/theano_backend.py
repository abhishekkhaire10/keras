from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from collections import defaultdict
from contextlib import contextmanager
import theano
from theano import tensor as T
from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams
from theano.tensor.signal import pool
from theano.tensor.fft import rfft, irfft
from theano.printing import Print
from theano.tensor.signal.conv import conv2d as vec_conv
from theano.ifelse import ifelse

try:
    import theano.sparse as th_sparse_module
except ImportError:
    th_sparse_module = None
try:
    from theano.tensor.nnet.nnet import softsign as T_softsign
except ImportError:
    from theano.sandbox.softsign import softsign as T_softsign

import numpy as np
from .common import floatx, epsilon, image_data_format
from ..utils.generic_utils import has_arg
# Legacy functions
from .common import set_image_dim_ordering, image_dim_ordering

py_all = all
py_sum = sum

# INTERNAL UTILS
theano.config.floatX = floatx()
_LEARNING_PHASE = T.scalar(dtype='uint8', name='keras_learning_phase')  # 0 = test, 1 = train
_UID_PREFIXES = defaultdict(int)


def printing(x, string=''):
    """Prints `message` and the tensor value when evaluated.

     Note that `printing` returns a new tensor identical to `x`
     which should be used in the following code. Otherwise the
     print operation is not taken into account during evaluation.

     # Example
     ```python
         >>> x = K.printing(x, string="x is: ")
     ```

    # Arguments
        x: Tensor to print.
        string: Message to print jointly with the tensor.

    # Returns
        The same tensor `x`, unchanged.
    """
    return theano.printing.Print(string)(x)


def learning_phase():
    """Returns the learning phase flag.

    The learning phase flag is a bool tensor (0 = test, 1 = train)
    to be passed as input to any Keras function
    that uses a different behavior at train time and test time.

    # Returns
        Learning phase (scalar integer tensor or Python integer).
    """
    # False = test, True = train
    return _LEARNING_PHASE


def set_learning_phase(value):
    """Sets the learning phase to a fixed value.

    # Arguments
        value: Learning phase value, either 0 or 1 (integers).

    # Raises
        ValueError: if `value` is neither `0` nor `1`.
    """
    global _LEARNING_PHASE
    if value not in {0, 1}:
        raise ValueError('Expected learning phase to be '
                         '0 or 1.')
    _LEARNING_PHASE = value


def get_uid(prefix=''):
    """Provides a unique UID given a string prefix.

    # Arguments
        prefix: string.

    # Returns
        An integer.

    # Example
    ```python
        >>> keras.backend.get_uid('dense')
        1
        >>> keras.backend.get_uid('dense')
        2
    ```

    """
    _UID_PREFIXES[prefix] += 1
    return _UID_PREFIXES[prefix]


def reset_uids():
    """Resets UIDs to default
    :return:
    """
    global _UID_PREFIXES
    _UID_PREFIXES = defaultdict(int)


# VARIABLE MANIPULATION


def _assert_sparse_module():
    if not th_sparse_module:
        raise ImportError("Failed to import theano.sparse\n"
                          "You probably need to pip install nose-parameterized")


def is_sparse(tensor):
    """Returns whether a tensor is a sparse tensor.

    # Arguments
        tensor: A tensor instance.

    # Returns
        A boolean.

    # Example
    ```python
        >>> from keras import backend as K
        >>> a = K.placeholder((2, 2), sparse=False)
        >>> print(K.is_sparse(a))
        False
        >>> b = K.placeholder((2, 2), sparse=True)
        >>> print(K.is_sparse(b))
        True
    ```
    """
    return th_sparse_module and isinstance(tensor.type, th_sparse_module.SparseType)


def to_dense(tensor):
    """Converts a sparse tensor into a dense tensor and returns it.

    # Arguments
        tensor: A tensor instance (potentially sparse).

    # Returns
        A dense tensor.

    # Examples
    ```python
        >>> from keras import backend as K
        >>> b = K.placeholder((2, 2), sparse=True)
        >>> print(K.is_sparse(b))
        True
        >>> c = K.to_dense(b)
        >>> print(K.is_sparse(c))
        False
    ```
    """
    if is_sparse(tensor):
        return th_sparse_module.dense_from_sparse(tensor)
    else:
        return tensor


def _is_explicit_shape(shape):
    if hasattr(shape, '__iter__'):
        for x in shape:
            if x is not None:
                if not isinstance(x, int):
                    return False
        return True
    return False


NAME_SCOPE_STACK = []


@contextmanager
def name_scope(name):
    global NAME_SCOPE_STACK
    NAME_SCOPE_STACK.append(name)
    yield
    NAME_SCOPE_STACK.pop()


def _prepare_name(name, default):
    prefix = '/'.join(NAME_SCOPE_STACK)
    if name is None:
        return prefix + '/' + default
    return prefix + '/' + name


def variable(value, dtype=None, name=None, constraint=None):
    """Instantiates a variable and returns it.

    # Arguments
        value: Numpy array, initial value of the tensor.
        dtype: Tensor type.
        name: Optional name string for the tensor.
        constraint: Optional projection function to be
            applied to the variable after an optimizer update.

    # Returns
        A variable instance (with Keras metadata included).

    # Examples
    ```python
        >>> from keras import backend as K
        >>> val = np.array([[1, 2], [3, 4]])
        >>> kvar = K.variable(value=val, dtype='float64', name='example_var')
        >>> K.dtype(kvar)
        'float64'
        >>> print(kvar)
        example_var
        >>> K.eval(kvar)
        array([[ 1.,  2.],
               [ 3.,  4.]])
    ```
    """
    if dtype is None:
        dtype = floatx()
    if hasattr(value, 'tocoo'):
        _assert_sparse_module()
        variable = th_sparse_module.as_sparse_variable(
            value, name=_prepare_name(name, 'variable'))
    else:
        if isinstance(value, (theano.tensor.TensorVariable,
                              theano.tensor.sharedvar.TensorSharedVariable,
                              theano.tensor.TensorConstant)):
            # Support for RandomStreams().normal(), .uniform().
            value = value.eval()
        value = np.asarray(value, dtype=dtype)
        variable = theano.shared(value=value,
                                 name=_prepare_name(name, 'variable'),
                                 strict=False)
    variable._keras_shape = value.shape
    variable._uses_learning_phase = False
    variable.constraint = constraint
    return variable


def constant(value, dtype=None, shape=None, name=None):
    """Creates a constant tensor.

    # Arguments
        value: A constant value (or list)
        dtype: The type of the elements of the resulting tensor.
        shape: Optional dimensions of resulting tensor.
        name: Optional name for the tensor.

    # Returns
        A Constant Tensor.
    """
    if dtype is None:
        dtype = floatx()
    if shape is None:
        shape = ()
    np_value = value * np.ones(shape)
    const = T.constant(np_value,
                       dtype=dtype,
                       name=_prepare_name(name, 'constant'))
    const._keras_shape = shape
    const._uses_learning_phase = False
    return const


def is_keras_tensor(x):
    """Returns whether `x` is a Keras tensor.

    A "Keras tensor" is a tensor that was returned by a Keras layer,
    (`Layer` class) or by `Input`.

    # Arguments
        x: A candidate tensor.

    # Returns
        A boolean: Whether the argument is a Keras tensor.

    # Raises
        ValueError: In case `x` is not a symbolic tensor.

    # Examples
    ```python
        >>> from keras import backend as K
        >>> from keras.layers import Input, Dense
        >>> np_var = numpy.array([1, 2])
        >>> K.is_keras_tensor(np_var) # A numpy array is not a symbolic tensor.
        ValueError
        >>> k_var = K.placeholder('float32', shape=(1,1))
        >>> K.is_keras_tensor(k_var) # A variable indirectly created outside of keras is not a Keras tensor.
        False
        >>> keras_var = K.variable(np_var)
        >>> K.is_keras_tensor(keras_var)  # A variable created with the keras backend is not a Keras tensor.
        False
        >>> keras_placeholder = K.placeholder(shape=(2, 4, 5))
        >>> K.is_keras_tensor(keras_placeholder)  # A placeholder is not a Keras tensor.
        False
        >>> keras_input = Input([10])
        >>> K.is_keras_tensor(keras_input) # An Input is a Keras tensor.
        True
        >>> keras_layer_output = Dense(10)(keras_input)
        >>> K.is_keras_tensor(keras_layer_output) # Any Keras layer output is a Keras tensor.
        True
    ```
    """
    if not isinstance(x, (T.TensorVariable,
                          T.sharedvar.TensorSharedVariable)):
        raise ValueError('Unexpectedly found an instance of type `' + str(type(x)) + '`. '
                                                                                     'Expected a symbolic tensor instance.')
    return hasattr(x, '_keras_history')


def placeholder(shape=None, ndim=None, dtype=None, sparse=False, name=None):
    """Instantiates a placeholder tensor and returns it.

    # Arguments
        shape: Shape of the placeholder
            (integer tuple, may include `None` entries).
        ndim: Number of axes of the tensor.
            At least one of {`shape`, `ndim`} must be specified.
            If both are specified, `shape` is used.
        dtype: Placeholder type.
        sparse: Boolean, whether the placeholder should have a sparse type.
        name: Optional name string for the placeholder.

    # Returns
        Tensor instance (with Keras metadata included).

    # Examples
    ```python
        >>> from keras import backend as K
        >>> input_ph = K.placeholder(shape=(2, 4, 5))
        >>> input_ph._keras_shape
        (2, 4, 5)
    ```
    """
    if dtype is None:
        dtype = floatx()
    if shape is None and ndim is None:
        raise ValueError('Specify either a shape or ndim value.')
    if shape is not None:
        ndim = len(shape)
    else:
        shape = tuple([None for _ in range(ndim)])

    name = _prepare_name(name, 'placeholder')
    broadcast = (False,) * ndim
    if sparse:
        _assert_sparse_module()
        x = th_sparse_module.csr_matrix(name=name, dtype=dtype)
    else:
        x = T.TensorType(dtype, broadcast)(name)
    x._keras_shape = shape
    x._uses_learning_phase = False
    x._theano_placeholder = True
    return x


def is_placeholder(x):
    """Returns whether `x` is a placeholder.

    # Arguments
        x: A candidate placeholder.

    # Returns
        Boolean.
    """
    return hasattr(x, '_theano_placeholder') and x._theano_placeholder


def shape(x):
    """Returns the symbolic shape of a tensor or variable.

    # Arguments
        x: A tensor or variable.

    # Returns
        A symbolic shape (which is itself a tensor).

    # Examples
    ```python
        # TensorFlow example
        >>> from keras import backend as K
        >>> val = np.array([[1, 2], [3, 4]])
        >>> kvar = K.variable(value=val)
        >>> inputs = K.placeholder(shape=(2, 4, 5))
        >>> K.shape(kvar)
        Shape.0
        >>> K.shape(inputs)
        Shape.0
        # To get integer shape (Instead, you can use K.int_shape(x))
        >>> K.shape(kvar).eval()
        array([2, 2])
    ```
    """
    return x.shape


def int_shape(x):
    """Returns the shape of tensor or variable as a tuple of int or None entries.

    # Arguments
        x: Tensor or variable.

    # Returns
        A tuple of integers (or None entries).

    # Examples
    ```python
        >>> from keras import backend as K
        >>> inputs = K.placeholder(shape=(2, 4, 5))
        >>> K.int_shape(inputs)
        (2, 4, 5)
        >>> val = np.array([[1, 2], [3, 4]])
        >>> kvar = K.variable(value=val)
        >>> K.int_shape(kvar)
        (2, 2)
    ```
    """
    if hasattr(x, '_keras_shape'):
        return x._keras_shape
    else:
        raise TypeError('Not a Keras tensor:', x)


def ndim(x):
    """Returns the number of axes in a tensor, as an integer.

    # Arguments
        x: Tensor or variable.

    # Returns
        Integer (scalar), number of axes.

    # Examples
    ```python
        >>> from keras import backend as K
        >>> inputs = K.placeholder(shape=(2, 4, 5))
        >>> val = np.array([[1, 2], [3, 4]])
        >>> kvar = K.variable(value=val)
        >>> K.ndim(inputs)
        3
        >>> K.ndim(kvar)
        2
    ```
    """
    return x.ndim


def dtype(x):
    """Returns the dtype of a Keras tensor or variable, as a string.

    # Arguments
        x: Tensor or variable.

    # Returns
        String, dtype of `x`.

    # Examples
    ```python
        >>> from keras import backend as K
        >>> K.dtype(K.placeholder(shape=(2,4,5)))
        'float32'
        >>> K.dtype(K.placeholder(shape=(2,4,5), dtype='float32'))
        'float32'
        >>> K.dtype(K.placeholder(shape=(2,4,5), dtype='float64'))
        'float64'
        # Keras variable
        >>> kvar = K.variable(np.array([[1, 2], [3, 4]]))
        >>> K.dtype(kvar)
        'float32_ref'
        >>> kvar = K.variable(np.array([[1, 2], [3, 4]]), dtype='float32')
        >>> K.dtype(kvar)
        'float32_ref'
    ```
    """
    return x.dtype


def eval(x):
    """Evaluates the value of a variable.

    # Arguments
        x: A variable.

    # Returns
        A Numpy array.

    # Examples
    ```python
        >>> from keras import backend as K
        >>> kvar = K.variable(np.array([[1, 2], [3, 4]]), dtype='float32')
        >>> K.eval(kvar)
        array([[ 1.,  2.],
               [ 3.,  4.]], dtype=float32)
    ```
    """
    return to_dense(x).eval()


def zeros(shape, dtype=None, name=None):
    """Instantiates an all-zeros variable and returns it.

    # Arguments
        shape: Tuple of integers, shape of returned Keras variable
        dtype: String, data type of returned Keras variable
        name: String, name of returned Keras variable

    # Returns
        A variable (including Keras metadata), filled with `0.0`.
        Note that if `shape` was symbolic, we cannot return a variable,
        and will return a dynamically-shaped tensor instead.

    # Example
    ```python
        >>> from keras import backend as K
        >>> kvar = K.zeros((3,4))
        >>> K.eval(kvar)
        array([[ 0.,  0.,  0.,  0.],
               [ 0.,  0.,  0.,  0.],
               [ 0.,  0.,  0.,  0.]], dtype=float32)
    ```
    """
    if dtype is None:
        dtype = floatx()
    return variable(np.zeros(shape), dtype, name)


# Aliases
zeros_symbolic = zeros


def ones(shape, dtype=None, name=None):
    """Instantiates an all-ones variable and returns it.

    # Arguments
        shape: Tuple of integers, shape of returned Keras variable.
        dtype: String, data type of returned Keras variable.
        name: String, name of returned Keras variable.

    # Returns
        A Keras variable, filled with `1.0`.
        Note that if `shape` was symbolic, we cannot return a variable,
        and will return a dynamically-shaped tensor instead.

    # Example
    ```python
        >>> from keras import backend as K
        >>> kvar = K.ones((3,4))
        >>> K.eval(kvar)
        array([[ 1.,  1.,  1.,  1.],
               [ 1.,  1.,  1.,  1.],
               [ 1.,  1.,  1.,  1.]], dtype=float32)
    ```
    """
    if dtype is None:
        dtype = floatx()
    return variable(np.ones(shape), dtype, name)


def eye(size, dtype=None, name=None):
    """Instantiate an identity matrix and returns it.

    # Arguments
        size: Integer, number of rows/columns.
        dtype: String, data type of returned Keras variable.
        name: String, name of returned Keras variable.

    # Returns
        A Keras variable, an identity matrix.

    # Example
    ```python
        >>> from keras import backend as K
        >>> kvar = K.eye(3)
        >>> K.eval(kvar)
        array([[ 1.,  0.,  0.],
               [ 0.,  1.,  0.],
               [ 0.,  0.,  1.]], dtype=float32)
    ```

    """
    if dtype is None:
        dtype = floatx()
    return variable(np.eye(size), dtype, name)


def ones_like(x, dtype=None, name=None):
    """Instantiates an all-ones symbolic variable with the shape of x.
    """
    return T.ones_like(x, dtype=dtype)


def zeros_like(x, dtype=None, name=None):
    """Instantiates an all-zeros variable of the same shape as another tensor.

    # Arguments
        x: Keras variable or Keras tensor.
        dtype: String, dtype of returned Keras variable.
             None uses the dtype of x.
        name: String, name for the variable to create.

    # Returns
        A Keras variable with the shape of x filled with zeros.

    # Example
    ```python
        >>> from keras import backend as K
        >>> kvar = K.variable(np.random.random((2,3)))
        >>> kvar_zeros = K.zeros_like(kvar)
        >>> K.eval(kvar_zeros)
        array([[ 0.,  0.,  0.],
               [ 0.,  0.,  0.]], dtype=float32)
    ```
    """
    return T.zeros_like(x, dtype=dtype)


def identity(x, name=None):
    """Returns a tensor with the same content as the input tensor.

    # Arguments
        x: The input tensor.
        name: String, name for the variable to create.

    # Returns
        A tensor of the same shape, type and content.
    """
    return x.copy(name=name)


def random_uniform_variable(shape, low, high, dtype=None, name=None):
    """Instantiates a variable with values drawn from a uniform distribution.

    # Arguments
        shape: Tuple of integers, shape of returned Keras variable.
        low: Float, lower boundary of the output interval.
        high: Float, upper boundary of the output interval.
        dtype: String, dtype of returned Keras variable.
        name: String, name of returned Keras variable.
        seed: Integer, random seed.

    # Returns
        A Keras variable, filled with drawn samples.

    # Example
    ```python
        # TensorFlow example
        >>> kvar = K.random_uniform_variable((2,3), 0, 1)
        >>> kvar
        <tensorflow.python.ops.variables.Variable object at 0x10ab40b10>
        >>> K.eval(kvar)
        array([[ 0.10940075,  0.10047495,  0.476143  ],
               [ 0.66137183,  0.00869417,  0.89220798]], dtype=float32)
    ```
    """
    return variable(np.random.uniform(low=low, high=high, size=shape),
                    dtype=dtype, name=name)


def random_normal_variable(shape, mean, scale, dtype=None, name=None):
    """Instantiates a variable with values drawn from a normal distribution.

    # Arguments
        shape: Tuple of integers, shape of returned Keras variable.
        mean: Float, mean of the normal distribution.
        scale: Float, standard deviation of the normal distribution.
        dtype: String, dtype of returned Keras variable.
        name: String, name of returned Keras variable.
        seed: Integer, random seed.

    # Returns
        A Keras variable, filled with drawn samples.

    # Example
    ```python
        # TensorFlow example
        >>> kvar = K.random_normal_variable((2,3), 0, 1)
        >>> kvar
        <tensorflow.python.ops.variables.Variable object at 0x10ab12dd0>
        >>> K.eval(kvar)
        array([[ 1.19591331,  0.68685907, -0.63814116],
               [ 0.92629528,  0.28055015,  1.70484698]], dtype=float32)
    ```
    """
    return variable(np.random.normal(loc=0.0, scale=scale, size=shape),
                    dtype=dtype, name=name)


def count_params(x):
    """Returns the static number of elements in a Keras variable or tensor.

    # Arguments
        x: Keras variable or tensor.

    # Returns
        Integer, the number of elements in `x`, i.e., the product of the
        array's static dimensions.

    # Example
    ```python
        >>> kvar = K.zeros((2,3))
        >>> K.count_params(kvar)
        6
        >>> K.eval(kvar)
        array([[ 0.,  0.,  0.],
               [ 0.,  0.,  0.]], dtype=float32)
    ```
    """
    # We don't want those compilation to show up in Theano profiler.
    f = theano.function([], x.shape, profile=False)
    return np.prod(f())


def cast(x, dtype):
    """Casts a tensor to a different dtype and returns it.

    You can cast a Keras variable but it still returns a Keras tensor.

    # Arguments
        x: Keras tensor (or variable).
        dtype: String, either (`'float16'`, `'float32'`, or `'float64'`).

    # Returns
        Keras tensor with dtype `dtype`.

    # Example
    ```python
        >>> from keras import backend as K
        >>> input = K.placeholder((2, 3), dtype='float32')
        >>> input
        /placeholder
        # It doesn't work in-place as below.
        >>> K.cast(input, dtype='float16')
        Elemwise{ceil,no_inplace}.0
        >>> input
        /placeholder
        # you need to assign it.
        >>> input = K.cast(input, dtype='float16')
        >>> input
        Elemwise{ceil,no_inplace}.0
    ```
    """
    return T.cast(x, dtype)


def ceil(x, name=None):
    """Ceils the value of `x`.

    # Arguments
        x: A `Variable`.
        name: Name of the new (ceiled) `Variable`.

    # Returns
         `Variable` `x` ceiled.
    """
    return T.ceil(x)


def floor(x):
    """Floors the value of `x`.

    # Arguments
        x: A `Variable`.
        name: Name of the new (floored) `Variable`.

    # Returns
         `Variable` `x` floored.
    """
    return T.floor(x)


# UPDATES OPS


def update(x, new_x):
    """Update the value of `x` to `new_x`.

    # Arguments
        x: A `Variable`.
        new_x: A tensor of same shape as `x`.

    # Returns
        The variable `x` updated.
    """
    return (x, new_x)


def update_add(x, increment):
    """Update the value of `x` by adding `increment`.

    # Arguments
        x: A `Variable`.
        increment: A tensor of same shape as `x`.

    # Returns
        The variable `x` updated.
    """
    return (x, x + increment)


def update_sub(x, decrement):
    """Update the value of `x` by subtracting `decrement`.

    # Arguments
        x: A `Variable`.
        decrement: A tensor of same shape as `x`.

    # Returns
        The variable `x` updated.
    """
    return (x, x - decrement)


def moving_average_update(variable, value, momentum):
    """Compute the moving average of a variable.

    # Arguments
        x: A `Variable`.
        value: A tensor with the same shape as `x`.
        momentum: The moving average momentum.

    # Returns
        An operation to update the variable.
    """
    return (variable, variable * momentum + value * (1. - momentum))


# LINEAR ALGEBRA

"""
Assumed overridden:
+, -, /, *, +=, -=, *=, /=
"""


def dot(x, y):
    """Multiplies 2 tensors (and/or variables) and returns a *tensor*.

    When attempting to multiply a nD tensor
    with a nD tensor, it reproduces the Theano behavior.
    (e.g. `(2, 3) * (4, 3, 5) -> (2, 4, 5)`)

    # Arguments
        x: Tensor or variable.
        y: Tensor or variable.

    # Returns
        A tensor, dot product of `x` and `y`.

    # Examples
    ```python
        # dot product between tensors
        >>> x = K.placeholder(shape=(2, 3))
        >>> y = K.placeholder(shape=(3, 4))
        >>> xy = K.dot(x, y)
        >>> xy
        dot.0
    ```

    ```python
        # dot product between tensors
        >>> x = K.placeholder(shape=(32, 28, 3))
        >>> y = K.placeholder(shape=(3, 4))
        >>> xy = K.dot(x, y)
        >>> xy
        Reshape{3}.0
    ```

    ```python
        # Theano-like behavior example
        >>> x = K.random_uniform_variable(shape=(2, 3), low=0, high=1)
        >>> y = K.ones((4, 3, 5))
        >>> xy = K.dot(x, y)
        >>> K.int_shape(xy)
        (2, 4, 5)
    ```
    """
    if is_sparse(x):
        out = th_sparse_module.basic.structured_dot(x, y)
    else:
        out = T.dot(x, y)
    if hasattr(x, '_keras_shape') and hasattr(y, '_keras_shape'):
        x_shape = list(x._keras_shape)
        y_shape = list(y._keras_shape)
        if len(x_shape) > 0:
            x_shape.pop()
        if len(y_shape) == 1:
            y_shape.pop()
        elif len(y_shape) > 1:
            y_shape.pop(-2)
        out._keras_shape = tuple(x_shape + y_shape)
    return out


def batch_dot(x, y, axes=None):
    """Batchwise dot product.

    `batch_dot` is used to compute dot product of `x` and `y` when
    `x` and `y` are data in batch, i.e. in a shape of
    `(batch_size, :)`.
    `batch_dot` results in a tensor or variable with less dimensions
    than the input. If the number of dimensions is reduced to 1,
    we use `expand_dims` to make sure that ndim is at least 2.

    # Arguments
        x: Keras tensor or variable with `ndim >= 2`.
        y: Keras tensor or variable with `ndim >= 2`.
        axes: list of (or single) int with target dimensions.
            The lengths of `axes[0]` and `axes[1]` should be the same.

    # Returns
        A tensor with shape equal to the concatenation of `x`'s shape
        (less the dimension that was summed over) and `y`'s shape
        (less the batch dimension and the dimension that was summed over).
        If the final rank is 1, we reshape it to `(batch_size, 1)`.

    # Examples
        Assume `x = [[1, 2], [3, 4]]` and `y = [[5, 6], [7, 8]]`
        `batch_dot(x, y, axes=1) = [[17], [53]]` which is the main diagonal
        of `x.dot(y.T)`, although we never have to calculate the off-diagonal
        elements.

        Shape inference:
        Let `x`'s shape be `(100, 20)` and `y`'s shape be `(100, 30, 20)`.
        If `axes` is (1, 2), to find the output shape of resultant tensor,
            loop through each dimension in `x`'s shape and `y`'s shape:

        * `x.shape[0]` : 100 : append to output shape
        * `x.shape[1]` : 20 : do not append to output shape,
            dimension 1 of `x` has been summed over. (`dot_axes[0]` = 1)
        * `y.shape[0]` : 100 : do not append to output shape,
            always ignore first dimension of `y`
        * `y.shape[1]` : 30 : append to output shape
        * `y.shape[2]` : 20 : do not append to output shape,
            dimension 2 of `y` has been summed over. (`dot_axes[1]` = 2)
        `output_shape` = `(100, 30)`

    ```python
        >>> x_batch = K.ones(shape=(32, 20, 1))
        >>> y_batch = K.ones(shape=(32, 30, 20))
        >>> xy_batch_dot = K.batch_dot(x_batch, y_batch, axes=[1, 2])
        >>> K.int_shape(xy_batch_dot)
        (32, 1, 30)
    ```
    """
    if isinstance(axes, int):
        axes = (axes, axes)
    if axes is None:
        # behaves like tf.batch_matmul as default
        axes = [x.ndim - 1, y.ndim - 2]
    out = T.batched_tensordot(x, y, axes=axes)
    if ndim(out) == 1:
        out = expand_dims(out, 1)

    if hasattr(x, '_keras_shape') and hasattr(y, '_keras_shape'):
        shape = []
        for axis in range(len(x._keras_shape)):
            if axis != axes[0]:
                shape.append(x._keras_shape[axis])
        for axis in range(1, len(y._keras_shape)):
            if axis != axes[1]:
                shape.append(y._keras_shape[axis])
        if len(shape) == 1:
            shape.append(1)  # Expand dims if ndim == 1
        out._keras_shape = tuple(shape)
    return out


def dot_product(x, kernel):
    """Wrapper for dot product operation, in order to be compatible with both Theano and Tensorflow.

    # Arguments:
        x: input
        kernel: weights

    # Returns
        A tensor.
    """
    return dot(x, kernel)


def transpose(x):
    """Transposes a tensor and returns it.

    # Arguments
        x: Tensor or variable.

    # Returns
        A tensor.

    # Examples
    ```python
        >>> var = K.variable([[1, 2, 3], [4, 5, 6]])
        >>> K.eval(var)
        array([[ 1.,  2.,  3.],
               [ 4.,  5.,  6.]], dtype=float32)
        >>> var_transposed = K.transpose(var)
        >>> K.eval(var_transposed)
        array([[ 1.,  4.],
               [ 2.,  5.],
               [ 3.,  6.]], dtype=float32)
    ```

    ```python
        >>> inputs = K.placeholder((2, 3))
        >>> inputs
        >>> input_transposed = K.transpose(inputs)
        >>> input_transposed

    ```
    """
    y = T.transpose(x)
    if hasattr(x, '_keras_shape'):
        y._keras_shape = tuple(reversed(x._keras_shape))
    return y


def gather(reference, indices):
    """Retrieves the elements of indices `indices` in the tensor `reference`.

    # Arguments
        reference: A tensor.
        indices: An integer tensor of indices.

    # Returns
        A tensor of same type as `reference`.
    """
    y = reference[indices]
    if hasattr(reference, '_keras_shape') and hasattr(indices, '_keras_shape'):
        y._keras_shape = indices._keras_shape + reference._keras_shape[1:]
    return y


def fft(x, norm=None):
    """Fast fourier transform:
       Compute an n-point fft of frames along given axis.
    """
    return rfft(x, norm=norm)


def ifft(x, norm=None, is_odd=False):
    """Inverse fast fourier transform
    """
    return irfft(x, norm=norm, is_odd=is_odd)


def real(x):
    """Gets the real part of a complex tensor
    """
    return T.real(x)


# ELEMENT-WISE OPERATIONS


def max(x, axis=None, keepdims=False):
    """Maximum value in a tensor.

    # Arguments
        x: A tensor or variable.
        axis: An integer, the axis to find maximum values.
        keepdims: A boolean, whether to keep the dimensions or not.
            If `keepdims` is `False`, the rank of the tensor is reduced
            by 1. If `keepdims` is `True`,
            the reduced dimension is retained with length 1.

    # Returns
        A tensor with maximum values of `x`.
    """
    return T.max(x, axis=axis, keepdims=keepdims)


def min(x, axis=None, keepdims=False):
    """Minimum value in a tensor.

    # Arguments
        x: A tensor or variable.
        axis: An integer, the axis to find minimum values.
        keepdims: A boolean, whether to keep the dimensions or not.
            If `keepdims` is `False`, the rank of the tensor is reduced
            by 1. If `keepdims` is `True`,
            the reduced dimension is retained with length 1.

    # Returns
        A tensor with miminum values of `x`.
    """
    return T.min(x, axis=axis, keepdims=keepdims)


def sum(x, axis=None, keepdims=False):
    """Sum of the values in a tensor, alongside the specified axis.

    # Arguments
        x: A tensor or variable.
        axis: An integer, the axis to sum over.
        keepdims: A boolean, whether to keep the dimensions or not.
            If `keepdims` is `False`, the rank of the tensor is reduced
            by 1. If `keepdims` is `True`,
            the reduced dimension is retained with length 1.

    # Returns
        A tensor with sum of `x`.
    """
    return T.sum(x, axis=axis, keepdims=keepdims)


def prod(x, axis=None, keepdims=False):
    """Multiplies the values in a tensor, alongside the specified axis.

    # Arguments
        x: A tensor or variable.
        axis: An integer, the axis to compute the product.
        keepdims: A boolean, whether to keep the dimensions or not.
            If `keepdims` is `False`, the rank of the tensor is reduced
            by 1. If `keepdims` is `True`,
            the reduced dimension is retained with length 1.

    # Returns
        A tensor with the product of elements of `x`.
    """
    return T.prod(x, axis=axis, keepdims=keepdims)


def cumsum(x, axis=0):
    """Cumulative sum of the values in a tensor, alongside the specified axis.

    # Arguments
        x: A tensor or variable.
        axis: An integer, the axis to compute the sum.

    # Returns
        A tensor of the cumulative sum of values of `x` along `axis`.
    """
    return T.extra_ops.cumsum(x, axis=axis)


def cumprod(x, axis=0):
    """Cumulative product of the values in a tensor, alongside the specified axis.

    # Arguments
        x: A tensor or variable.
        axis: An integer, the axis to compute the product.

    # Returns
        A tensor of the cumulative product of values of `x` along `axis`.
    """
    return T.extra_ops.cumprod(x, axis=axis)


def var(x, axis=None, keepdims=False):
    """Variance of a tensor, alongside the specified axis.

    # Arguments
        x: A tensor or variable.
        axis: An integer, the axis to compute the variance.
        keepdims: A boolean, whether to keep the dimensions or not.
            If `keepdims` is `False`, the rank of the tensor is reduced
            by 1. If `keepdims` is `True`,
            the reduced dimension is retained with length 1.

    # Returns
        A tensor with the variance of elements of `x`.
    """
    return T.var(x, axis=axis, keepdims=keepdims)


def std(x, axis=None, keepdims=False):
    """Standard deviation of a tensor, alongside the specified axis.

    # Arguments
        x: A tensor or variable.
        axis: An integer, the axis to compute the standard deviation.
        keepdims: A boolean, whether to keep the dimensions or not.
            If `keepdims` is `False`, the rank of the tensor is reduced
            by 1. If `keepdims` is `True`,
            the reduced dimension is retained with length 1.

    # Returns
        A tensor with the standard deviation of elements of `x`.
    """
    return T.std(x, axis=axis, keepdims=keepdims)


def mean(x, axis=None, keepdims=False):
    """Mean of a tensor, alongside the specified axis.

    # Arguments
        x: A tensor or variable.
        axis: A list of integer. Axes to compute the mean.
        keepdims: A boolean, whether to keep the dimensions or not.
            If `keepdims` is `False`, the rank of the tensor is reduced
            by 1 for each entry in `axis`. If `keepdims` is `True`,
            the reduced dimensions are retained with length 1.

    # Returns
        A tensor with the mean of elements of `x`.
    """
    dtype = None
    # bool is available since theano v0.9dev
    if 'int' in x.dtype or x.dtype == 'bool':
        dtype = floatx()
    return T.mean(x, axis=axis, keepdims=keepdims, dtype=dtype)


def any(x, axis=None, keepdims=False):
    """Bitwise reduction (logical OR).

    # Arguments
        x: Tensor or variable.
        axis: axis along which to perform the reduction.
        keepdims: whether the drop or broadcast the reduction axes.

    # Returns
        A uint8 tensor (0s and 1s).
    """
    return T.any(x, axis=axis, keepdims=keepdims)


def all(x, axis=None, keepdims=False):
    """Bitwise reduction (logical AND).

    # Arguments
        x: Tensor or variable.
        axis: axis along which to perform the reduction.
        keepdims: whether the drop or broadcast the reduction axes.

    # Returns
        A uint8 tensor (0s and 1s).
    """
    return T.all(x, axis=axis, keepdims=keepdims)


def argmax(x, axis=-1):
    """Returns the index of the maximum value along an axis.

    # Arguments
        x: Tensor or variable.
        axis: axis along which to perform the reduction.

    # Returns
        A tensor.
    """
    return T.argmax(x, axis=axis, keepdims=False)


def argmin(x, axis=-1):
    """Returns the index of the minimum value along an axis.

    # Arguments
        x: Tensor or variable.
        axis: axis along which to perform the reduction.

    # Returns
        A tensor.
    """
    return T.argmin(x, axis=axis, keepdims=False)


def square(x):
    """Element-wise square.

    # Arguments
        x: Tensor or variable.

    # Returns
        A tensor.
    """
    return T.square(x)


def abs(x):
    """Element-wise absolute value.

    # Arguments
        x: Tensor or variable.

    # Returns
        A tensor.
    """
    return T.abs_(x)


def sqrt(x):
    """Element-wise square root.

    # Arguments
        x: Tensor or variable.

    # Returns
        A tensor.
    """
    x = T.clip(x, 0., np.inf)
    return T.sqrt(x)


def exp(x):
    """Element-wise exponential.

    # Arguments
        x: Tensor or variable.

    # Returns
        A tensor.
    """
    return T.exp(x)


def log(x):
    """Element-wise log.

    # Arguments
        x: Tensor or variable.

    # Returns
        A tensor.
    """
    return T.log(x)


def log2(x):
    """log in base 2
    """
    return T.log2(x)


def logsumexp(x, axis=None, keepdims=False):
    """Computes log(sum(exp(elements across dimensions of a tensor))).

    This function is more numerically stable than log(sum(exp(x))).
    It avoids overflows caused by taking the exp of large inputs and
    underflows caused by taking the log of small inputs.

    # Arguments
        x: A tensor or variable.
        axis: An integer, the axis to reduce over.
        keepdims: A boolean, whether to keep the dimensions or not.
            If `keepdims` is `False`, the rank of the tensor is reduced
            by 1. If `keepdims` is `True`, the reduced dimension is
            retained with length 1.

    # Returns
        The reduced tensor.
    """
    # Theano has a built-in optimization for logsumexp (see https://github.com/Theano/Theano/pull/4736)
    # so we can just write the expression directly:
    return T.log(T.sum(T.exp(x), axis=axis, keepdims=keepdims))


def round(x):
    """Element-wise rounding to the closest integer.

    In case of tie, the rounding mode used is "half to even".

    # Arguments
        x: Tensor or variable.

    # Returns
        A tensor.
    """
    return T.round(x, mode='half_to_even')


def sign(x):
    """Element-wise sign.

    # Arguments
        x: Tensor or variable.

    # Returns
        A tensor.
    """
    return T.sgn(x)


def pow(x, a):
    """Element-wise exponentiation.

    # Arguments
        x: Tensor or variable.
        a: Python integer.

    # Returns
        A tensor.
    """
    return T.pow(x, a)


def clip(x, min_value, max_value):
    """Element-wise value clipping.

    # Arguments
        x: Tensor or variable.
        min_value: Python float or integer.
        max_value: Python float or integer.

    # Returns
        A tensor.
    """
    if max_value is not None and max_value < min_value:
        max_value = min_value
    if max_value is None:
        max_value = np.inf
    return T.clip(x, min_value, max_value)


def equal(x, y):
    """Element-wise equality between two tensors.

    # Arguments
        x: Tensor or variable.
        y: Tensor or variable.

    # Returns
        A bool tensor.
    """
    return T.eq(x, y)


def not_equal(x, y):
    """Element-wise inequality between two tensors.

    # Arguments
        x: Tensor or variable.
        y: Tensor or variable.

    # Returns
        A bool tensor.
    """
    return T.neq(x, y)


def greater(x, y):
    """Element-wise truth value of (x > y).

    # Arguments
        x: Tensor or variable.
        y: Tensor or variable.

    # Returns
        A bool tensor.
    """
    return T.gt(x, y)


def greater_equal(x, y):
    """Element-wise truth value of (x >= y).

    # Arguments
        x: Tensor or variable.
        y: Tensor or variable.

    # Returns
        A bool tensor.
    """
    return T.ge(x, y)


def less(x, y):
    """Element-wise truth value of (x < y).

    # Arguments
        x: Tensor or variable.
        y: Tensor or variable.

    # Returns
        A bool tensor.
    """
    return T.lt(x, y)


def less_equal(x, y):
    """Element-wise truth value of (x <= y).

    # Arguments
        x: Tensor or variable.
        y: Tensor or variable.

    # Returns
        A bool tensor.
    """
    return T.le(x, y)


def maximum(x, y):
    """Element-wise maximum of two tensors.

    # Arguments
        x: Tensor or variable.
        y: Tensor or variable.

    # Returns
        A tensor.
    """
    return T.maximum(x, y)


def minimum(x, y):
    """Element-wise minimum of two tensors.

    # Arguments
        x: Tensor or variable.
        y: Tensor or variable.

    # Returns
        A tensor.
    """
    return T.minimum(x, y)


def sin(x):
    """Computes sin of x element-wise.

    # Arguments
        x: Tensor or variable.

    # Returns
        A tensor.
    """
    return T.sin(x)


def cos(x):
    """Computes cos of x element-wise.

    # Arguments
        x: Tensor or variable.

    # Returns
        A tensor.
    """
    return T.cos(x)


def normalize_batch_in_training(x, gamma, beta,
                                reduction_axes, epsilon=1e-3):
    """Computes mean and std for batch then apply batch_normalization on batch.
    """
    # TODO remove this if statement when Theano without
    # T.nnet.bn.batch_normalization_train is deprecated
    if not hasattr(T.nnet.bn, 'batch_normalization_train'):
        return _old_normalize_batch_in_training(x, gamma, beta, reduction_axes, epsilon)

    if gamma is None:
        if beta is None:
            gamma = ones_like(x)
        else:
            gamma = ones_like(beta)
    if beta is None:
        if gamma is None:
            beta = zeros_like(x)
        beta = zeros_like(gamma)

    normed, mean, stdinv = T.nnet.bn.batch_normalization_train(
        x, gamma, beta, reduction_axes, epsilon)

    return normed, mean, T.inv(stdinv ** 2)


def batch_normalization(x, mean, var, beta, gamma, epsilon=1e-3):
    """Apply batch normalization on x given mean, var, beta and gamma.
    """
    # TODO remove this if statement when Theano without
    # T.nnet.bn.batch_normalization_test is deprecated
    if not hasattr(T.nnet.bn, 'batch_normalization_test'):
        return _old_batch_normalization(x, mean, var, beta, gamma, epsilon)

    if gamma is None:
        gamma = ones_like(var)
    if beta is None:
        beta = zeros_like(mean)

    if mean.ndim == 1:
        # based on TensorFlow's default: normalize along rightmost dimension
        reduction_axes = list(range(x.ndim - 1))
    else:
        reduction_axes = [i for i in range(x.ndim) if mean.broadcastable[i]]

    return T.nnet.bn.batch_normalization_test(
        x, gamma, beta, mean, var, reduction_axes, epsilon)


# TODO remove this function when Theano without
# T.nnet.bn.batch_normalization_train is deprecated
def _old_normalize_batch_in_training(x, gamma, beta,
                                     reduction_axes, epsilon=1e-3):
    """Computes mean and std for batch then apply batch_normalization on batch.
    """
    if gamma is None:
        gamma = ones_like(x)
    if beta is None:
        beta = zeros_like(x)

    dev = theano.config.device
    use_cudnn = ndim(x) < 5 and reduction_axes == [0, 2, 3] and (dev.startswith('cuda') or dev.startswith('gpu'))
    if use_cudnn:
        broadcast_beta = beta.dimshuffle('x', 0, 'x', 'x')
        broadcast_gamma = gamma.dimshuffle('x', 0, 'x', 'x')
        try:
            normed, mean, stdinv = theano.sandbox.cuda.dnn.dnn_batch_normalization_train(
                x, broadcast_gamma, broadcast_beta, 'spatial', epsilon)
            normed = theano.tensor.as_tensor_variable(normed)
            mean = theano.tensor.as_tensor_variable(mean)
            stdinv = theano.tensor.as_tensor_variable(stdinv)
            var = T.inv(stdinv ** 2)
            return normed, T.flatten(mean), T.flatten(var)
        except AttributeError:
            pass

    var = x.var(reduction_axes)
    mean = x.mean(reduction_axes)

    target_shape = []
    for axis in range(ndim(x)):
        if axis in reduction_axes:
            target_shape.append(1)
        else:
            target_shape.append(x.shape[axis])
    target_shape = T.stack(*target_shape)

    broadcast_mean = T.reshape(mean, target_shape)
    broadcast_var = T.reshape(var, target_shape)
    broadcast_beta = T.reshape(beta, target_shape)
    broadcast_gamma = T.reshape(gamma, target_shape)
    normed = batch_normalization(x, broadcast_mean, broadcast_var,
                                 broadcast_beta, broadcast_gamma,
                                 epsilon)
    return normed, mean, var


# TODO remove this if statement when Theano without
# T.nnet.bn.batch_normalization_test is deprecated
def _old_batch_normalization(x, mean, var, beta, gamma, epsilon=1e-3):
    """Apply batch normalization on x given mean, var, beta and gamma.
    """
    if gamma is None:
        gamma = ones_like(var)
    if beta is None:
        beta = zeros_like(mean)

    if mean.ndim == 1 and x.ndim > 1:
        # in TensorFlow's batch_normalization, if the parameters are vectors
        # the batch normalization should be applied along the rightmost axis.
        # Theano expects the parameters to always have x.ndim dimensions.
        shuffle_pattern = ['x'] * (x.ndim - 1) + [0]
        mean = mean.dimshuffle(shuffle_pattern)
        var = var.dimshuffle(shuffle_pattern)
        beta = beta.dimshuffle(shuffle_pattern)
        gamma = gamma.dimshuffle(shuffle_pattern)

    ndim = x.ndim
    dev = theano.config.device
    use_cudnn = ndim < 5 and (dev.startswith('cuda') or dev.startswith('gpu'))
    if use_cudnn:
        try:
            axis = mean.broadcastable.index(False)
            if axis != 1:
                shuffle_pattern = list(range(ndim))
                shuffle_pattern[1] = shuffle_pattern[axis]
                shuffle_pattern[axis] = 1
                result = theano.sandbox.cuda.dnn.dnn_batch_normalization_test(
                    x.dimshuffle(shuffle_pattern),
                    gamma.dimshuffle(shuffle_pattern),
                    beta.dimshuffle(shuffle_pattern),
                    mean.dimshuffle(shuffle_pattern),
                    var.dimshuffle(shuffle_pattern),
                    'spatial', epsilon).dimshuffle(shuffle_pattern)
            else:
                result = theano.sandbox.cuda.dnn.dnn_batch_normalization_test(
                    x, gamma, beta, mean, var, 'spatial', epsilon)
            return theano.tensor.as_tensor_variable(result)
        except AttributeError:
            pass
        except ValueError:
            pass
    return T.nnet.bn.batch_normalization(x, gamma, beta, mean, sqrt(var + epsilon),
                                         mode='high_mem')


# SHAPE OPERATIONS

def concatenate(tensors, axis=-1):
    """Concatenates a list of tensors alongside the specified axis.

    # Arguments
        tensors: list of tensors to concatenate.
        axis: concatenation axis.

    # Returns
        A tensor.
    """
    if py_all([is_sparse(x) for x in tensors]):
        axis = axis % ndim(tensors[0])
        if axis == 0:
            return th_sparse_module.basic.vstack(tensors, format='csr')
        elif axis == 1:
            return th_sparse_module.basic.hstack(tensors, format='csr')
        else:
            raise ValueError('Invalid concat axis for sparse matrix:', axis)
    else:
        return T.concatenate([to_dense(x) for x in tensors], axis=axis)


def reshape(x, shape):
    """Reshapes a tensor to the specified shape.

    # Arguments
        x: Tensor or variable.
        shape: Target shape tuple.

    # Returns
        A tensor.
    """
    y = T.reshape(x, shape)
    if _is_explicit_shape(shape):
        shape = tuple(x if x != -1 else None for x in shape)
        y._keras_shape = shape
        if hasattr(x, '_uses_learning_phase'):
            y._uses_learning_phase = x._uses_learning_phase
        else:
            y._uses_learning_phase = False
    return y


def permute_dimensions(x, pattern):
    """Permutes axes in a tensor.

    # Arguments
        x: Tensor or variable.
        pattern: A tuple of
            dimension indices, e.g. `(0, 2, 1)`.

    # Returns
        A tensor.
    """
    pattern = tuple(pattern)
    y = x.dimshuffle(pattern)
    if hasattr(x, '_keras_shape'):
        y._keras_shape = tuple(np.asarray(x._keras_shape)[list(pattern)])
    return y


def repeat_elements(x, rep, axis):
    """Repeat the elements of a tensor along an axis, like np.repeat.

    If x has shape (s1, s2, s3) and axis=1, the output
    will have shape (s1, s2 * rep, s3).
    """
    y = T.repeat(x, rep, axis=axis)
    if hasattr(x, '_keras_shape'):
        y._keras_shape = list(x._keras_shape)
        repeat_dim = x._keras_shape[axis]
        if repeat_dim is not None:
            y._keras_shape[axis] = repeat_dim * rep
        y._keras_shape = tuple(y._keras_shape)
    return y


def resize_images(x, height_factor, width_factor, data_format):
    """Resizes the images contained in a 4D tensor.

    # Arguments
        x: Tensor or variable to resize.
        height_factor: Positive integer.
        width_factor: Positive integer.
        data_format: string, `"channels_last"` or `"channels_first"`.

    # Returns
        A tensor.

    # Raises
        ValueError: if `data_format` is neither `"channels_last"` or `"channels_first"`.
    """
    if data_format == 'channels_first':
        output = repeat_elements(x, height_factor, axis=2)
        output = repeat_elements(output, width_factor, axis=3)
        return output
    elif data_format == 'channels_last':
        output = repeat_elements(x, height_factor, axis=1)
        output = repeat_elements(output, width_factor, axis=2)
        return output
    else:
        raise ValueError('Invalid data_format:', data_format)


def resize_volumes(x, depth_factor, height_factor, width_factor, data_format):
    """Resizes the volume contained in a 5D tensor.

    # Arguments
        x: Tensor or variable to resize.
        depth_factor: Positive integer.
        height_factor: Positive integer.
        width_factor: Positive integer.
        data_format: string, `"channels_last"` or `"channels_first"`.

    # Returns
        A tensor.

    # Raises
        ValueError: if `data_format` is neither `"channels_last"` or `"channels_first"`.
    """
    if data_format == 'channels_first':
        output = repeat_elements(x, depth_factor, axis=2)
        output = repeat_elements(output, height_factor, axis=3)
        output = repeat_elements(output, width_factor, axis=4)
        return output
    elif data_format == 'channels_last':
        output = repeat_elements(x, depth_factor, axis=1)
        output = repeat_elements(output, height_factor, axis=2)
        output = repeat_elements(output, width_factor, axis=3)
        return output
    else:
        raise ValueError('Invalid data_format:', data_format)


def repeat(x, n):
    """Repeats a 2D tensor.

    if `x` has shape (samples, dim) and `n` is `2`,
    the output will have shape `(samples, 2, dim)`.

    # Arguments
        x: Tensor or variable.
        n: Python integer, number of times to repeat.

    # Returns
        A tensor.
    """
    assert x.ndim == 2
    y = x.dimshuffle((0, 'x', 1))
    y = T.extra_ops.repeat(y, n, axis=1)
    if hasattr(x, '_keras_shape'):
        shape = list(x._keras_shape)
        shape.insert(1, n)
        y._keras_shape = tuple(shape)

    return y


def repeatRdim(x, n, axis=1):
    """Repeats an RD tensor.

    If x has shape (samples, dim1, dim2) and n=2 and axis=1,
    the output will have shape (samples, 2, dim1, dim2).
    """
    new_dim = range(axis) + ['x'] + range(axis, x.ndim)
    x = x.dimshuffle(tuple(new_dim))
    return T.extra_ops.repeat(x, n, axis=axis)


def set_subtensor(x, v):
    """Returns x with the given subtensor overwritten by v.

    # Arguments
        x: Tensor or variable.
        v: Tensor or variable.

    # Returns
        The tensor `x` overwritten by `v`.
    """
    return T.set_subtensor(x, v)


def inc_subtensor(x, v):
    """Returns x with the given subtensor incremented by v.

    # Arguments
        x: Tensor or variable.
        v: Tensor or variable.

    # Returns
        The tensor `x` incremented by `v`.
    """
    return T.inc_subtensor(x, v)


def equal_dimensions(x, y):
    """Checks if `x` has the same dimensions than `y`.

    # Arguments
        x: Tensor or variable.
        y: Tensor or variable.

    # Returns
        True if `x` has the same dimensions than `y`, False otherwise.
    """
    y_shape = int_shape(y)
    x_shape = int_shape(x)
    fun_comp = x_shape[2] == y_shape[2] and x_shape[3] == y_shape[3]
    return ifelse(fun_comp, y, funequal(x, y))


def funequal(x, y):
    """Utility for `equal_dimensions`.

    # Arguments
        x: Tensor or variable.
        y: Tensor or variable.

    # Returns
        A tensor
    """
    new_y = zeros([1, 1, 1, 1])
    new_y = set_subtensor(new_y[:, :, :-1, :-1], y)
    return new_y


def arange(start, stop=None, step=1, dtype='int32'):
    """Creates a 1D tensor containing a sequence of integers.

    The function arguments use the same convention as
    Theano's arange: if only one argument is provided,
    it is in fact the "stop" argument.

    The default type of the returned tensor is `'int32'` to
    match TensorFlow's default.

    # Arguments
        start: Start value.
        stop: Stop value.
        step: Difference between two successive values.
        dtype: Integer dtype to use.

    # Returns
        An integer tensor.

    """
    return T.arange(start, stop=stop, step=step, dtype=dtype)


def tile(x, n):
    """Creates a tensor by tiling `x` by `n`.

    # Arguments
        x: A tensor or variable
        n: A list of integer. The length must be the same as the number of
            dimensions in `x`.

    # Returns
        A tiled tensor.
    """
    y = T.tile(x, n)
    if hasattr(x, '_keras_shape'):
        if _is_explicit_shape(n):
            output_shape = x._keras_shape[:-len(n)]
            for i, j in zip(x._keras_shape, n):
                if i is None:
                    output_shape += (None,)
                else:
                    output_shape += (i * j,)
        elif isinstance(n, int):
            output_shape = x._keras_shape[:-1]
            if x._keras_shape[-1] is None:
                output_shape += (None,)
            else:
                output_shape += (x._keras_shape[-1] * n,)
        else:
            # symbolic n
            if n.ndim == 0:
                # n is a scalar
                output_shape = x._keras_shape[:-1] + (None,)
            elif hasattr(n, '_keras_shape'):
                # n is a vector
                n_size = n._keras_shape[0]
                output_shape = x._keras_shape[:-n_size] + (None,) * n_size
            else:
                output_shape = (None,) * x.ndim
        y._keras_shape = output_shape
    return y


def flatten(x):
    """Flatten a tensor.

    # Arguments
        x: A tensor or variable.

    # Returns
        A tensor, reshaped into 1-D
    """
    y = T.flatten(x)
    if hasattr(x, '_keras_shape'):
        if None in x._keras_shape:
            y._keras_shape = (None,)
        else:
            y._keras_shape = (np.prod(x._keras_shape),)
    return y


def batch_flatten(x):
    """Turn a nD tensor into a 2D tensor with same 0th dimension.

    In other words, it flattens each data samples of a batch.

    # Arguments
        x: A tensor or variable.

    # Returns
        A tensor.
    """
    y = T.reshape(x, (x.shape[0], T.prod(x.shape[1:])))
    if hasattr(x, '_keras_shape'):
        if None in x._keras_shape[1:]:
            y._keras_shape = (x._keras_shape[0], None)
        else:
            y._keras_shape = (x._keras_shape[0], np.prod(x._keras_shape[1:]))
    return y


def expand_dims(x, axis=-1):
    """Adds a 1-sized dimension at index "axis".

    # Arguments
        x: A tensor or variable.
        axis: Position where to add a new axis.

    # Returns
        A tensor with expanded dimensions.
    """
    pattern = [i for i in range(x.type.ndim)]
    if axis < 0:
        if x.type.ndim == 0:
            axis = 0
        else:
            axis = axis % x.type.ndim + 1
    pattern.insert(axis, 'x')
    y = x.dimshuffle(pattern)
    if hasattr(x, '_keras_shape'):
        shape = list(x._keras_shape)
        shape.insert(axis, 1)
        y._keras_shape = tuple(shape)
    return y


def squeeze(x, axis):
    """Removes a 1-dimension from the tensor at index "axis".

    # Arguments
        x: A tensor or variable.
        axis: Axis to drop.

    # Returns
        A tensor with the same data as `x` but reduced dimensions.
    """
    shape = list(x.shape)
    shape.pop(axis)
    y = T.reshape(x, tuple(shape))
    if hasattr(x, '_keras_shape'):
        kshape = list(x._keras_shape)
        kshape.pop(axis)
        y._keras_shape = tuple(kshape)
    return y


def temporal_padding(x, padding=(1, 1)):
    """Pads the middle dimension of a 3D tensor.

    # Arguments
        x: Tensor or variable.
        padding: Tuple of 2 integers, how many zeros to
            add at the start and end of dim 1.

    # Returns
        A padded 3D tensor.
    """
    assert len(padding) == 2
    input_shape = x.shape
    output_shape = (input_shape[0],
                    input_shape[1] + padding[0] + padding[1],
                    input_shape[2])
    output = T.zeros(output_shape)
    result = T.set_subtensor(output[:, padding[0]:x.shape[1] + padding[0], :], x)
    if hasattr(x, '_keras_shape'):
        result._keras_shape = (x._keras_shape[0],
                               x._keras_shape[1] + py_sum(padding),
                               x._keras_shape[2])
    return result


def spatial_2d_padding(x, padding=((1, 1), (1, 1)), data_format=None):
    """Pads the 2nd and 3rd dimensions of a 4D tensor.

    # Arguments
        x: Tensor or variable.
        padding: Tuple of 2 tuples, padding pattern.
        data_format: string, `"channels_last"` or `"channels_first"`.

    # Returns
        A padded 4D tensor.

    # Raises
        ValueError: if `data_format` is neither `"channels_last"` or `"channels_first"`.
    """
    assert len(padding) == 2
    assert len(padding[0]) == 2
    assert len(padding[1]) == 2
    top_pad, bottom_pad = padding[0]
    left_pad, right_pad = padding[1]
    if data_format is None:
        data_format = image_data_format()
    if data_format not in {'channels_first', 'channels_last'}:
        raise ValueError('Unknown data_format ' + str(data_format))

    input_shape = x.shape
    if data_format == 'channels_first':
        output_shape = (input_shape[0],
                        input_shape[1],
                        input_shape[2] + top_pad + bottom_pad,
                        input_shape[3] + left_pad + right_pad)
        output = T.zeros(output_shape)
        indices = (slice(None),
                   slice(None),
                   slice(top_pad, input_shape[2] + top_pad),
                   slice(left_pad, input_shape[3] + left_pad))

    else:
        output_shape = (input_shape[0],
                        input_shape[1] + top_pad + bottom_pad,
                        input_shape[2] + left_pad + right_pad,
                        input_shape[3])
        output = T.zeros(output_shape)
        indices = (slice(None),
                   slice(top_pad, input_shape[1] + top_pad),
                   slice(left_pad, input_shape[2] + left_pad),
                   slice(None))
    y = T.set_subtensor(output[indices], x)
    y._keras_shape = output_shape
    return y


def spatial_3d_padding(x, padding=((1, 1), (1, 1), (1, 1)), data_format=None):
    """Pads 5D tensor with zeros along the depth, height, width dimensions.

    Pads these dimensions with respectively
    "padding[0]", "padding[1]" and "padding[2]" zeros left and right.

    For 'channels_last' data_format,
    the 2nd, 3rd and 4th dimension will be padded.
    For 'channels_first' data_format,
    the 3rd, 4th and 5th dimension will be padded.

    # Arguments
        x: Tensor or variable.
        padding: Tuple of 3 tuples, padding pattern.
        data_format: string, `"channels_last"` or `"channels_first"`.

    # Returns
        A padded 5D tensor.

    # Raises
        ValueError: if `data_format` is neither `"channels_last"` or `"channels_first"`.

    """
    if data_format is None:
        data_format = image_data_format()
    if data_format not in {'channels_first', 'channels_last'}:
        raise ValueError('Unknown data_format ' + str(data_format))

    input_shape = x.shape
    if data_format == 'channels_first':
        output_shape = (input_shape[0],
                        input_shape[1],
                        input_shape[2] + padding[0][0] + padding[0][1],
                        input_shape[3] + padding[1][0] + padding[1][1],
                        input_shape[4] + padding[2][0] + padding[2][1])
        output = T.zeros(output_shape)
        indices = (slice(None),
                   slice(None),
                   slice(padding[0][0], input_shape[2] + padding[0][0]),
                   slice(padding[1][0], input_shape[3] + padding[1][0]),
                   slice(padding[2][0], input_shape[4] + padding[2][0]))

    else:
        output_shape = (input_shape[0],
                        input_shape[1] + padding[0][0] + padding[0][1],
                        input_shape[2] + padding[1][0] + padding[1][1],
                        input_shape[3] + padding[2][0] + padding[2][1],
                        input_shape[4])
        output = T.zeros(output_shape)
        indices = (slice(None),
                   slice(padding[0][0], input_shape[1] + padding[0][0]),
                   slice(padding[1][0], input_shape[2] + padding[1][0]),
                   slice(padding[2][0], input_shape[3] + padding[2][0]),
                   slice(None))
    return T.set_subtensor(output[indices], x)


def stack(x, axis=0):
    """Stacks a list of rank `R` tensors into a rank `R+1` tensor.

    # Arguments
        x: List of tensors.
        axis: Axis along which to perform stacking.

    # Returns
        A tensor.
    """
    return T.stack(x, axis=axis)


def one_hot(indices, num_classes):
    """Computes the one-hot representation of an integer tensor.

    # Arguments
        indices: nD integer tensor of shape
            `(batch_size, dim1, dim2, ... dim(n-1))`
        num_classes: Integer, number of classes to consider.

    # Returns
        (n + 1)D one hot representation of the input
        with shape `(batch_size, dim1, dim2, ... dim(n-1), num_classes)`
    """
    input_shape = tuple((indices.shape[i] for i in range(indices.ndim)))
    indices = T.flatten(indices)
    oh = T.extra_ops.to_one_hot(indices, num_classes)
    oh = T.reshape(oh, input_shape + (num_classes,))
    return oh


def reverse(x, axes):
    """Reverse a tensor along the specified axes.

    # Arguments
        x: Tensor to reverse.
        axes: Integer or iterable of integers.
            Axes to reverse.

    # Returns
        A tensor.
    """
    if isinstance(axes, int):
        axes = [axes]
    slices = [slice(None, None, -1) if i in axes else slice(None, None, None) for i in range(x.ndim)]
    return x[slices]


def pattern_broadcast(x, broadcastable):
    """Make the input adopt a specific broadcasting pattern.
    """
    return T.patternbroadcast(x, broadcastable)


# VALUE MANIPULATION


def get_value(x):
    """Returns the value of a variable.

    # Arguments
        x: input variable.

    # Returns
        A Numpy array.
    """
    if isinstance(x, np.ndarray):
        return x
    if not hasattr(x, 'get_value'):
        raise TypeError('`get_value` can only be called on a variable. '
                        'If you have an expression instead, use `eval()`.')
    return x.get_value()


def batch_get_value(xs):
    """Returns the value of more than one tensor variable.

    # Arguments
        ops: list of ops to run.

    # Returns
        A list of Numpy arrays.
    """
    return [get_value(x) for x in xs]


def set_value(x, value):
    """Sets the value of a variable, from a Numpy array.

    # Arguments
        x: Tensor to set to a new value.
        value: Value to set the tensor to, as a Numpy array
            (of the same shape).
    """
    x.set_value(np.asarray(value, dtype=x.dtype))


def batch_set_value(tuples):
    """Sets the values of many tensor variables at once.

    # Arguments
        tuples: a list of tuples `(tensor, value)`.
            `value` should be a Numpy array.
    """
    for x, value in tuples:
        x.set_value(np.asarray(value, dtype=x.dtype))


def get_variable_shape(x):
    """Returns the shape of a variable.

    # Arguments
        x: A variable.

    # Returns
        A tuple of integers.
    """
    return x.get_value(borrow=True, return_internal_type=True).shape


def print_tensor(x, message=''):
    """Prints `message` and the tensor value when evaluated.

     Note that `print_tensor` returns a new tensor identical to `x`
     which should be used in the following code. Otherwise the
     print operation is not taken into account during evaluation.

     # Example
     ```python
         >>> x = K.print_tensor(x, message="x is: ")
     ```

    # Arguments
        x: Tensor to print.
        message: Message to print jointly with the tensor.

    # Returns
        The same tensor `x`, unchanged.
    """
    p_op = Print(message)
    return p_op(x)


# GRAPH MANIPULATION

class Function(object):
    """Wrapper around Theano Function
    """

    def __init__(self, inputs, outputs, updates=[], name=None, **kwargs):
        unique_variables_to_update = {}
        for v, nv in updates:
            if v not in unique_variables_to_update:
                unique_variables_to_update[v] = nv
        updates = unique_variables_to_update.items()
        self.function = theano.function(inputs, outputs, updates=updates,
                                        allow_input_downcast=True,
                                        on_unused_input='ignore',
                                        name=name,
                                        **kwargs)
        self.name = name

    def __call__(self, inputs):
        assert isinstance(inputs, (list, tuple))
        return self.function(*inputs)


def function(inputs, outputs, updates=[], **kwargs):
    """Return a :class:`callable object <theano.compile.function_module.Function>`
    that will calculate `outputs` from `inputs`.
    """
    if len(kwargs) > 0:
        for key in kwargs.keys():
            if not has_arg(theano.function, key, True):
                msg = 'Invalid argument "%s" passed to K.function with Theano backend' % key
                raise ValueError(msg)
    return Function(inputs, outputs, updates=updates, **kwargs)


def gradients(loss, variables):
    """Return symbolic gradients of one cost with respect to one or more variables.
    """
    return T.grad(loss, variables)


def stop_gradient(variables):
    """Returns `variables` but with zero gradient w.r.t. every other variable.

    # Arguments
        variables: tensor or list of tensors to consider constant with respect
            to any other variable.

    # Returns
        A single tensor or a list of tensors (depending on the passed argument)
            that has constant gradient with respect to any other variable.
    """
    if isinstance(variables, (list, tuple)):
        return map(theano.gradient.disconnected_grad, variables)
    else:
        return theano.gradient.disconnected_grad(variables)


# CONTROL FLOW

def rnn(step_function, inputs, initial_states,
        go_backwards=False, mask=None, constants=None,
        unroll=False, input_length=None, pos_extra_outputs_states=None):
    """Iterates over the time dimension of a tensor.

    # Arguments
        step_function: RNN step function.
            Parameters:
                inputs: tensor with shape `(samples, ...)` (no time dimension),
                    representing input for the batch of samples at a certain
                    time step.
                states: list of tensors.
            Returns:
                outputs: tensor with shape `(samples, units)`
                    (no time dimension).
                new_states: list of tensors, same length and shapes
                    as 'states'. The first state in the list must be the
                    output tensor at the previous timestep.
        inputs: tensor of temporal data of shape `(samples, time, ...)`
            (at least 3D).
        initial_states: tensor with shape (samples, units)
            (no time dimension),
            containing the initial values for the states used in
            the step function.
        go_backwards: boolean. If True, do the iteration over the time
            dimension in reverse order and return the reversed sequence.
        mask: binary tensor with shape `(samples, time, 1)`,
            with a zero for every element that is masked.
        constants: a list of constant values passed at each step.
        unroll: whether to unroll the RNN or to use a symbolic loop (`while_loop` or `scan` depending on backend).
        input_length: not relevant in the TensorFlow implementation.
            Must be specified if using unrolling with Theano.
        pos_extra_outputs_states: Positions that extra_output_states will have.

    # Returns
        A tuple, `(last_output, outputs, new_states)`.

            last_output: the latest output of the rnn, of shape `(samples, ...)`
            outputs: tensor with shape `(samples, time, ...)` where each
                entry `outputs[s, t]` is the output of the step function
                at time `t` for sample `s`.
            new_states: list of tensors, latest states returned by
                the step function, of shape `(samples, ...)`.

    # Raises
        ValueError: if input dimension is less than 3.
        ValueError: if `unroll` is `True` but input timestep is not a fixed number.
        ValueError: if `mask` is provided (not `None`) but states is not provided
            (`len(states)` == 0).
    """
    ndim = inputs.ndim
    assert ndim >= 3, 'Input should be at least 3D.'

    if unroll:
        if input_length is None:
            raise ValueError('When specifying `unroll=True`, '
                             'an `input_length` '
                             'must be provided to `rnn`.')

    axes = [1, 0] + list(range(2, ndim))
    inputs = inputs.dimshuffle(axes)

    if constants is None:
        constants = []

    global uses_learning_phase
    uses_learning_phase = False

    if mask is not None:
        if mask.ndim < ndim:
            mask = expand_dims(mask)
        mask = mask.dimshuffle([1, 0] + list(range(2, mask.ndim)))

        if unroll:
            indices = list(range(input_length))
            if go_backwards:
                indices = indices[::-1]

            successive_outputs = []
            successive_states = []
            states = initial_states
            for i in indices:
                output, new_states = step_function(inputs[i], states + constants)
                if getattr(output, '_uses_learning_phase', False):
                    uses_learning_phase = True

                if len(successive_outputs) == 0:
                    prev_output = zeros_like(output)
                else:
                    prev_output = successive_outputs[-1]

                output = T.switch(mask[i], output, prev_output)
                kept_states = []
                for state, new_state in zip(states, new_states):
                    kept_states.append(T.switch(mask[i], new_state, state))
                states = kept_states

                successive_outputs.append(output)
                successive_states.append(states)

            outputs = T.stack(*successive_outputs)
            states = []
            for i in range(len(successive_states[-1])):
                states.append(T.stack(*[states_at_step[i] for states_at_step in successive_states]))
        else:
            # build an all-zero tensor of shape (samples, output_dim)
            initial_output = step_function(inputs[0], initial_states + constants)[0] * 0
            # Theano gets confused by broadcasting patterns in the scan op
            initial_output = T.unbroadcast(initial_output, 0, 1)

            def _step(inputs, mask, output_tm1, *states):
                outputs, new_states = step_function(inputs, states)
                if getattr(outputs, '_uses_learning_phase', False):
                    global uses_learning_phase
                    uses_learning_phase = True
                # output previous output if masked.
                outputs = T.switch(mask, outputs, output_tm1)
                return_states = []
                for state, new_state in zip(states, new_states):
                    # TODO: Theano cannot optimize this and therefore, it shows the InconsistencyError (new backend)
                    return_states.append(T.switch(mask, new_state, state))
                return [outputs] + return_states

            results, _ = theano.scan(
                _step,
                sequences=[inputs, mask],
                outputs_info=[initial_output] + initial_states,
                non_sequences=constants,
                go_backwards=go_backwards)

            # deal with Theano API inconsistency
            if isinstance(results, list):
                outputs = results[0]
                states = results[1:]
            else:
                outputs = results
                states = []
    else:
        if unroll:
            indices = list(range(input_length))
            if go_backwards:
                indices = indices[::-1]

            successive_outputs = []
            successive_states = []
            states = initial_states
            for i in indices:
                outputs, states = step_function(inputs[i], states + constants)
                if getattr(outputs, '_uses_learning_phase', False):
                    uses_learning_phase = True
                successive_outputs.append(outputs)
                successive_states.append(states)
            outputs = T.stack(*successive_outputs)
            states = []
            for i in range(len(successive_states[-1])):
                states.append(T.stack(*[states_at_step[i] for states_at_step in successive_states]))

        else:
            def _step(inputs, *states):
                outputs, new_states = step_function(inputs, states)
                if getattr(outputs, '_uses_learning_phase', False):
                    global uses_learning_phase
                    uses_learning_phase = True
                return [outputs] + new_states

            results, _ = theano.scan(
                _step,
                sequences=inputs,
                outputs_info=[None] + initial_states,
                non_sequences=constants,
                go_backwards=go_backwards)

            # deal with Theano API inconsistency
            if isinstance(results, list):
                outputs = results[0]
                states = results[1:]
            else:
                outputs = results
                states = []

    outputs = T.squeeze(outputs)
    last_output = outputs[-1]

    axes = [1, 0] + list(range(2, outputs.ndim))
    outputs = outputs.dimshuffle(axes)

    if pos_extra_outputs_states is None:
        states = [T.squeeze(state[-1]) for state in states]
    else:
        states = [state if i_s in pos_extra_outputs_states
                  else T.squeeze(state[-1]) for i_s, state in enumerate(states)]
    last_output._uses_learning_phase = uses_learning_phase
    return last_output, outputs, states


def switch(condition, then_expression, else_expression):
    """Switches between two operations depending on a scalar value.

    Note that both `then_expression` and `else_expression`
    should be symbolic tensors of the *same shape*.

    # Arguments
        condition: scalar tensor (`int` or `bool`).
        then_expression: either a tensor, or a callable that returns a tensor.
        else_expression: either a tensor, or a callable that returns a tensor.

    # Returns
        The selected tensor.
    """
    if callable(then_expression):
        then_expression = then_expression()
    if callable(else_expression):
        else_expression = else_expression()
    cond_ndim = ndim(condition)
    expr_ndim = ndim(then_expression)
    if cond_ndim < expr_ndim:
        ndim_diff = expr_ndim - cond_ndim
        for _ in range(ndim_diff):
            condition = expand_dims(condition)
    return T.switch(condition, then_expression, else_expression)


def in_train_phase(x, alt, training=None):
    """Selects `x` in train phase, and `alt` otherwise.

    Note that `alt` should have the *same shape* as `x`.

    # Arguments
        x: What to return in train phase
            (tensor or callable that returns a tensor).
        alt: What to return otherwise
            (tensor or callable that returns a tensor).
        training: Optional scalar tensor
            (or Python boolean, or Python integer)
            specifying the learning phase.

    # Returns
        Either `x` or `alt` based on the `training` flag.
        the `training` flag defaults to `K.learning_phase()`.
    """
    if training is None:
        training = learning_phase()
        uses_learning_phase = True
    else:
        uses_learning_phase = False

    if training is 1 or training is True:
        if callable(x):
            return x()
        else:
            return x

    elif training is 0 or training is False:
        if callable(alt):
            return alt()
        else:
            return alt

    if callable(x):
        x = x()
    if callable(alt):
        alt = alt()

    # else: assume learning phase is a placeholder tensor.
    x = ifelse(training, x, alt)
    if uses_learning_phase:
        x._uses_learning_phase = True
    return x


def in_test_phase(x, alt, training=None):
    """Selects `x` in test phase, and `alt` otherwise.

    Note that `alt` should have the *same shape* as `x`.

    # Arguments
        x: What to return in test phase
            (tensor or callable that returns a tensor).
        alt: What to return otherwise
            (tensor or callable that returns a tensor).
        training: Optional scalar tensor
            (or Python boolean, or Python integer)
            specifying the learning phase.

    # Returns
        Either `x` or `alt` based on `K.learning_phase`.
    """
    return in_train_phase(alt, x, training=training)


# NN OPERATIONS

def _assert_has_capability(module, func):
    if not hasattr(module, func):
        raise EnvironmentError(
            'It looks like like your version of '
            'Theano is out of date. '
            'Install the latest version with:\n'
            'pip install git+git://github.com/Theano/Theano.git '
            '--upgrade --no-deps')


def relu(x, alpha=0., max_value=None):
    """Rectified linear unit.

    With default values, it returns element-wise `max(x, 0)`.

    # Arguments
        x: A tensor or variable.
        alpha: A scalar, slope of negative section (default=`0.`).
        max_value: Saturation threshold.

    # Returns
        A tensor.
    """
    _assert_has_capability(T.nnet, 'relu')
    x = T.nnet.relu(x, alpha)
    if max_value is not None:
        x = T.minimum(x, max_value)
    return x


def elu(x, alpha=1.0):
    """Exponential linear unit.

    # Arguments
        x: A tensor or variable to compute the activation function for.
        alpha: A scalar, slope of negative section.

    # Returns
        A tensor.
    """
    _assert_has_capability(T.nnet, 'elu')
    return T.nnet.elu(x, alpha)


def softmax(x):
    """Softmax of a tensor.

    # Arguments
        x: A tensor or variable.

    # Returns
        A tensor.
    """
    return T.nnet.softmax(x)


def softmax_3d(x):
    """"Softmax on the last axis of a 2d or 3d tensor.

    # Arguments
        x: A tensor or variable.

    # Returns
        A tensor.

    # Raises
        Exception: If the input tensor is not 2D or 3D.
    """
    nd = ndim(x)
    if nd == 2:
        return softmax(x)
    elif nd == 3:
        e = exp(x - max(x, axis=-1, keepdims=True))
        s = sum(e, axis=-1, keepdims=True)
        return e / s
    else:
        raise Exception('Cannot apply softmax to a tensor that is not 2D or 3D. ' +
                        'Here, ndim=' + str(nd))


def softplus(x):
    """Softplus of a tensor.

    # Arguments
        x: A tensor or variable.

    # Returns
        A tensor.
    """
    return T.nnet.softplus(x)


def softsign(x):
    """Softsign of a tensor.

    # Arguments
        x: A tensor or variable.

    # Returns
        A tensor.
    """
    return T_softsign(x)


def categorical_crossentropy(target, output, from_logits=False):
    """Categorical crossentropy between an output tensor and a target tensor.

    # Arguments
        target: A tensor of the same shape as `output`.
        output: A tensor resulting from a softmax
            (unless `from_logits` is True, in which
            case `output` is expected to be the logits).
        from_logits: Boolean, whether `output` is the
            result of a softmax, or is a tensor of logits.

    # Returns
        Output tensor.
    """
    if from_logits:
        output = T.nnet.softmax(output)
    else:
        # scale preds so that the class probas of each sample sum to 1
        output /= output.sum(axis=-1, keepdims=True)
    # avoid numerical instability with _EPSILON clipping
    output = T.clip(output, epsilon(), 1.0 - epsilon())
    return T.nnet.categorical_crossentropy(output, target)


def sparse_categorical_crossentropy(target, output, from_logits=False):
    """Categorical crossentropy with integer targets.

    # Arguments
        target: An integer tensor.
        output: A tensor resulting from a softmax
            (unless `from_logits` is True, in which
            case `output` is expected to be the logits).
        from_logits: Boolean, whether `output` is the
            result of a softmax, or is a tensor of logits.

    # Returns
        Output tensor.
    """
    target = T.cast(T.flatten(target), 'int32')
    target = T.extra_ops.to_one_hot(target, nb_class=output.shape[-1])
    target = reshape(target, shape(output))
    return categorical_crossentropy(target, output, from_logits)


def binary_crossentropy(target, output, from_logits=False):
    """Binary crossentropy between an output tensor and a target tensor.

    # Arguments
        target: A tensor with the same shape as `output`.
        output: A tensor.
        from_logits: Whether `output` is expected to be a logits tensor.
            By default, we consider that `output`
            encodes a probability distribution.

    # Returns
        A tensor.
    """
    if from_logits:
        output = T.nnet.sigmoid(output)
    # avoid numerical instability with _EPSILON clipping
    output = T.clip(output, epsilon(), 1.0 - epsilon())
    return T.nnet.binary_crossentropy(output, target)


def weighted_binary_crossentropy(target, output, from_logits=False, lambda_w_rec=1.0, lambda_w_pre=1.0):
    """Weighted crossentropy of binary random variables.

    # Arguments
        target: A tensor with the same shape as `output`.
        output: A tensor.
        from_logits: Whether `output` is expected to be a logits tensor.
            By default, we consider that `output`
            encodes a probability distribution.
        lambda_w_rec: Float. First weight.
        lambda_w_pre: Float. Second weight.

    # Returns
        A tensor.
    """
    if from_logits:
        output = T.nnet.sigmoid(output)
    # avoid numerical instability with _epsilon clipping
    output = T.clip(output, _epsilon(), 1.0 - _epsilon())
    return -(lambda_w_rec * target * T.log(output) + lambda_w_pre * (1.0 - target) * T.log(1.0 - output))


def sigmoid(x):
    """Element-wise sigmoid.

    # Arguments
        x: A tensor or variable.

    # Returns
        A tensor.
    """
    return T.nnet.sigmoid(x)


def hard_sigmoid(x):
    """Segment-wise linear approximation of sigmoid.

    Faster than sigmoid.
    Returns `0.` if `x < -2.5`, `1.` if `x > 2.5`.
    In `-2.5 <= x <= 2.5`, returns `0.2 * x + 0.5`.

    # Arguments
        x: A tensor or variable.

    # Returns
        A tensor.
    """
    return T.nnet.hard_sigmoid(x)


def tanh(x):
    """Element-wise tanh.

    # Arguments
        x: A tensor or variable.

    # Returns
        A tensor.
    """
    return T.tanh(x)


def dropout(x, level, noise_shape=None, seed=None):
    """Sets entries in `x` to zero at random, while scaling the entire tensor.

    # Arguments
        x: tensor
        level: fraction of the entries in the tensor
            that will be set to 0.
        noise_shape: shape for randomly generated keep/drop flags,
            must be broadcastable to the shape of `x`
        seed: random seed to ensure determinism.

    # Returns
        A tensor.
    """
    if level < 0. or level >= 1:
        raise ValueError('Dropout level must be in interval [0, 1[.')
    if seed is None:
        seed = np.random.randint(1, 10e6)
    if isinstance(noise_shape, list):
        noise_shape = tuple(noise_shape)

    rng = RandomStreams(seed=seed)
    retain_prob = 1. - level
    if noise_shape is None:
        random_tensor = rng.binomial(x.shape, p=retain_prob, dtype=x.dtype)
    else:
        random_tensor = rng.binomial(noise_shape, p=retain_prob, dtype=x.dtype)
        random_tensor = T.patternbroadcast(random_tensor,
                                           [dim == 1 for dim in noise_shape])
    x *= random_tensor
    x /= retain_prob
    return x


def l2_normalize(x, axis=None):
    """Normalizes a tensor wrt the L2 norm alongside the specified axis.

    # Arguments
        x: Tensor or variable.
        axis: axis along which to perform normalization.

    # Returns
        A tensor.
    """
    square_sum = T.sum(T.square(x), axis=axis, keepdims=True)
    norm = T.sqrt(T.maximum(square_sum, epsilon()))
    return x / norm


def l1_normalize(x, axis):
    """Normalizes a tensor wrt the L1 norm alongside the specified axis.

    # Arguments
        x: Tensor or variable.
        axis: axis along which to perform normalization.

    # Returns
        A tensor.
    """
    norm = T.max(T.sum(abs(x), axis=axis, keepdims=True))
    return x / norm


def in_top_k(predictions, targets, k):
    """Returns whether the `targets` are in the top `k` `predictions`.

    # Arguments
        predictions: A tensor of shape `(batch_size, classes)` and type `float32`.
        targets: A 1D tensor of length `batch_size` and type `int32` or `int64`.
        k: An `int`, number of top elements to consider.

    # Returns
        A 1D tensor of length `batch_size` and type `bool`.
        `output[i]` is `True` if `predictions[i, targets[i]]` is within top-`k`
        values of `predictions[i]`.
    """
    # handle k < 1 and k >= predictions.shape[1] cases to match TF behavior
    if k < 1:
        # dtype='bool' is only available since Theano 0.9.0
        try:
            return T.zeros_like(targets, dtype='bool')
        except TypeError:
            return T.zeros_like(targets, dtype='int8')

    if k >= int_shape(predictions)[1]:
        try:
            return T.ones_like(targets, dtype='bool')
        except TypeError:
            return T.ones_like(targets, dtype='int8')

    predictions_k = T.sort(predictions)[:, -k]
    targets_values = predictions[T.arange(targets.shape[0]), targets]
    return T.ge(targets_values, predictions_k)


# CONVOLUTIONS

def _preprocess_conv2d_input(x, data_format):
    """Transpose and cast the input before the conv2d.

    # Arguments
        x: input tensor.
        data_format: string, `"channels_last"` or `"channels_first"`.

    # Returns
        A tensor.
    """
    if data_format == 'channels_last':
        # TF uses the last dimension as channel dimension,
        # instead of the 2nd one.
        # TH input shape: (samples, input_depth, rows, cols)
        # TF input shape: (samples, rows, cols, input_depth)
        x = x.dimshuffle((0, 3, 1, 2))
    return x


def _preprocess_conv3d_input(x, data_format):
    """Transpose and cast the input before the conv3d.

    # Arguments
        x: input tensor.
        data_format: string, `"channels_last"` or `"channels_first"`.

    # Returns
        A tensor.
    """
    if data_format == 'channels_last':
        # TF uses the last dimension as channel dimension,
        # instead of the 2nd one.
        # TH input shape: (samples, input_depth, rows, cols, slices)
        # TF input shape: (samples, rows, cols, slices, input_depth)
        x = x.dimshuffle((0, 4, 1, 2, 3))
    return x


def _preprocess_conv2d_kernel(kernel, data_format):
    # As of Keras 2.0.0, all kernels are normalized
    # on the format `(rows, cols, input_depth, depth)`,
    # independently of `data_format`.
    # Theano expects `(depth, input_depth, rows, cols)`.
    kernel = kernel.dimshuffle((3, 2, 0, 1))
    return kernel


def _preprocess_conv3d_kernel(kernel, data_format):
    # As of Keras 2.0.0, all kernels are normalized
    # on the format `(space, input_depth, depth)`,
    # independently of `data_format`.
    # Theano expects `(depth, input_depth, space)`.
    kernel = kernel.dimshuffle((4, 3, 0, 1, 2))
    return kernel


def _preprocess_padding(padding):
    """Convert keras' padding to theano's padding.

    # Arguments
        padding: string, `"same"` or `"valid"`.

    # Returns
        a string, `"SAME"` or `"VALID"`.

    # Raises
        ValueError: if `padding` is invalid.
    """
    if padding == 'same':
        th_padding = 'half'
    elif padding == 'valid':
        th_padding = 'valid'
    elif padding == 'full':
        th_padding = 'full'
    else:
        raise ValueError('Border mode not supported:', str(padding))
    return th_padding


def _preprocess_conv2d_image_shape(image_shape, data_format):
    # Theano might not accept long type
    def int_or_none(value):
        try:
            return int(value)
        except TypeError:
            return None

    if data_format == 'channels_last':
        if image_shape:
            image_shape = (image_shape[0], image_shape[3],
                           image_shape[1], image_shape[2])
    if image_shape is not None:
        image_shape = tuple(int_or_none(v) for v in image_shape)
    return image_shape


def _preprocess_conv3d_volume_shape(volume_shape, data_format):
    # Theano might not accept long type
    def int_or_none(value):
        try:
            return int(value)
        except TypeError:
            return None

    if data_format == 'channels_last':
        if volume_shape:
            volume_shape = (volume_shape[0], volume_shape[4],
                            volume_shape[1], volume_shape[2], volume_shape[3])
    if volume_shape is not None:
        volume_shape = tuple(int_or_none(v) for v in volume_shape)
    return volume_shape


def _preprocess_conv2d_filter_shape(filter_shape, data_format):
    # Theano might not accept long type
    def int_or_none(value):
        try:
            return int(value)
        except TypeError:
            return None

    if filter_shape:
        filter_shape = (filter_shape[3], filter_shape[2],
                        filter_shape[0], filter_shape[1])
    if filter_shape is not None:
        filter_shape = tuple(int_or_none(v) for v in filter_shape)
    return filter_shape


def _preprocess_conv3d_filter_shape(filter_shape, data_format):
    # Theano might not accept long type
    def int_or_none(value):
        try:
            return int(value)
        except TypeError:
            return None

    if filter_shape:
        filter_shape = (filter_shape[4], filter_shape[3],
                        filter_shape[0], filter_shape[1], filter_shape[2])
    if filter_shape is not None:
        filter_shape = tuple(int_or_none(v) for v in filter_shape)
    return filter_shape


def _postprocess_conv2d_output(conv_out, x,
                               padding, kernel_shape,
                               strides, data_format):
    if padding == 'same':
        if kernel_shape[2] % 2 == 0:
            conv_out = conv_out[:, :, :(x.shape[2] + strides[0] - 1) // strides[0], :]
        if kernel_shape[3] % 2 == 0:
            conv_out = conv_out[:, :, :, :(x.shape[3] + strides[1] - 1) // strides[1]]
    if data_format == 'channels_last':
        conv_out = conv_out.dimshuffle((0, 2, 3, 1))
    return conv_out


def _postprocess_conv3d_output(conv_out, x,
                               padding, kernel_shape,
                               strides, data_format):
    if padding == 'same':
        if kernel_shape[2] % 2 == 0:
            conv_out = conv_out[:, :, :(x.shape[2] + strides[0] - 1) // strides[0], :, :]
        if kernel_shape[3] % 2 == 0:
            conv_out = conv_out[:, :, :, :(x.shape[3] + strides[1] - 1) // strides[1], :]
        if kernel_shape[4] % 2 == 0:
            conv_out = conv_out[:, :, :, :, :(x.shape[4] + strides[2] - 1) // strides[2]]
    if data_format == 'channels_last':
        conv_out = conv_out.dimshuffle((0, 2, 3, 4, 1))
    return conv_out


def conv1d(x, kernel, strides=1, padding='valid',
           data_format=None, dilation_rate=1):
    """1D convolution.

    # Arguments
        x: Tensor or variable.
        kernel: kernel tensor.
        strides: stride integer.
        padding: string, `"same"`, `"causal"` or `"valid"`.
        data_format: string, `"channels_last"` or `"channels_first"`.
        dilation_rate: integer dilate rate.

    # Returns
        A tensor, result of 1D convolution.

    # Raises
        ValueError: if `data_format` is neither `channels_last` or `channels_first`.
    """
    if data_format is None:
        data_format = image_data_format()
    if data_format not in {'channels_first', 'channels_last'}:
        raise ValueError('Unknown data_format ', data_format)

    if hasattr(kernel, '_keras_shape'):
        kernel_shape = kernel._keras_shape
    else:
        kernel_shape = None
    if padding == 'causal':
        # causal (dilated) convolution:
        if not kernel_shape:
            raise AttributeError('Causal padding requires kernel._keras_shape set.')
        left_pad = dilation_rate * (kernel_shape[0] - 1)
        x = temporal_padding(x, (left_pad, 0))
        padding = 'valid'
    if hasattr(x, '_keras_shape'):
        shape = x._keras_shape
    else:
        shape = None
    if data_format == 'channels_last':
        # original shape: (batch, length, input_dim)
        # add dim to x to have (batch, length, 1, input_dim)
        x = expand_dims(x, 2)
        # update x._keras_shape
        if shape is not None:
            x._keras_shape = (shape[0], shape[1], 1, shape[2])
    else:
        # original shape: (batch, input_dim, length)
        # add dim to x to have (batch, input_dim, length, 1)
        x = expand_dims(x, 3)
        # update x._keras_shape
        if shape is not None:
            x._keras_shape = (shape[0], shape[1], shape[2], 1)
    # update dilation rate, strides
    dilation_rate = (dilation_rate, 1)
    strides = (strides, 1)
    # add dim to kernel (always same format independently of data_format)
    # i.e. (rows, 1, input_depth, depth)
    kernel = expand_dims(kernel, 1)
    output = conv2d(x, kernel,
                    strides=strides, padding=padding,
                    data_format=data_format, dilation_rate=dilation_rate)
    # remove added dim
    if data_format == 'channels_last':
        output = squeeze(output, 2)
    else:
        output = squeeze(output, 3)
    return output


def conv2d(x, kernel, strides=(1, 1), padding='valid',
           data_format=None, dilation_rate=(1, 1)):
    """2D convolution.

    # Arguments
        x: Tensor or variable.
        kernel: kernel tensor.
        strides: strides tuple.
        padding: string, `"same"` or `"valid"`.
        data_format: string, `"channels_last"` or `"channels_first"`.
            Whether to use Theano or TensorFlow/CNTK data format
            for inputs/kernels/outputs.
        dilation_rate: tuple of 2 integers.

    # Returns
        A tensor, result of 2D convolution.

    # Raises
        ValueError: if `data_format` is neither `channels_last` or `channels_first`.
    """
    if data_format is None:
        data_format = image_data_format()
    if data_format not in {'channels_first', 'channels_last'}:
        raise ValueError('Unknown data_format ', data_format)

    if hasattr(x, '_keras_shape'):
        image_shape = _preprocess_conv2d_image_shape(int_shape(x), data_format)
    else:
        image_shape = None
    if hasattr(kernel, '_keras_shape'):
        kernel_shape = kernel._keras_shape
    else:
        # Will only work if `kernel` is a shared variable.
        kernel_shape = kernel.eval().shape
    kernel_shape = _preprocess_conv2d_filter_shape(kernel_shape, data_format)

    x = _preprocess_conv2d_input(x, data_format)
    kernel = _preprocess_conv2d_kernel(kernel, data_format)
    th_padding = _preprocess_padding(padding)

    conv_out = T.nnet.conv2d(x, kernel,
                             border_mode=th_padding,
                             subsample=strides,
                             input_shape=image_shape,
                             filter_shape=kernel_shape,
                             filter_dilation=dilation_rate)
    conv_out = _postprocess_conv2d_output(conv_out, x, padding,
                                          kernel_shape, strides, data_format)
    return conv_out


def conv2d_transpose(x, kernel, output_shape, strides=(1, 1),
                     padding='valid', data_format=None):
    """2D deconvolution (i.e. transposed convolution).

    # Arguments
        x: Tensor or variable.
        kernel: kernel tensor.
        output_shape: 1D int tensor for the output shape.
        strides: strides tuple.
        padding: string, `"same"` or `"valid"`.
        data_format: string, `"channels_last"` or `"channels_first"`.
            Whether to use Theano or TensorFlow/CNTK data format
            for inputs/kernels/outputs.

    # Returns
        A tensor, result of transposed 2D convolution.

    # Raises
        ValueError: if `data_format` is neither `channels_last` or `channels_first`.
    """
    flip_filters = False
    if data_format is None:
        data_format = image_data_format()
    if data_format not in {'channels_first', 'channels_last'}:
        raise ValueError('Unknown data_format ' + data_format)

    if data_format == 'channels_last':
        output_shape = (output_shape[0],
                        output_shape[3],
                        output_shape[1],
                        output_shape[2])

    if hasattr(kernel, '_keras_shape'):
        kernel_shape = kernel._keras_shape
    else:
        # Will only work if `kernel` is a shared variable.
        kernel_shape = kernel.eval().shape

    if padding == 'same' and kernel_shape[0] % 2 == 0:
        raise ValueError('In `Conv2DTranspose`, with padding mode `same`, '
                         'even kernel sizes are not supported with Theano. '
                         'You can set `kernel_size` to an odd number.')

    kernel_shape = _preprocess_conv2d_filter_shape(kernel_shape, data_format)

    x = _preprocess_conv2d_input(x, data_format)
    kernel = _preprocess_conv2d_kernel(kernel, data_format)

    th_padding = _preprocess_padding(padding)
    op = T.nnet.abstract_conv.AbstractConv2d_gradInputs(imshp=None,
                                                        kshp=kernel_shape,
                                                        subsample=strides,
                                                        border_mode=th_padding,
                                                        filter_flip=not flip_filters)
    conv_out = op(kernel, x, output_shape[2:])
    conv_out = _postprocess_conv2d_output(conv_out, x, padding,
                                          kernel_shape, strides, data_format)

    # TODO: Check this (from v1.2)
    """
    # Compute output width/height if None
    output_size = [None] * 2
    for i in [-2, -1]:
        if output_shape[i] is None:
            output_size[i] = conv_input_length(x.shape[i], filter_shape[i],
                                               border_mode, strides[i])
        else:
            output_size[i] = output_shape[i]

    conv_out = op(kernel, x, output_size)
    conv_out = _postprocess_conv2d_output(conv_out, x, border_mode, np_kernel,
                                          strides, dim_ordering)
    """
    return conv_out


def separable_conv1d(x, depthwise_kernel, pointwise_kernel, strides=1,
                     padding='valid', data_format=None, dilation_rate=1):
    """1D convolution with separable filters.

    # Arguments
        x: input tensor
        depthwise_kernel: convolution kernel for the depthwise convolution.
        pointwise_kernel: kernel for the 1x1 convolution.
        strides: stride integer.
        padding: string, `"same"` or `"valid"`.
        data_format: string, `"channels_last"` or `"channels_first"`.
        dilation_rate: integer dilation rate.

    # Returns
        Output tensor.

    # Raises
        ValueError: if `data_format` is neither `channels_last` or `channels_first`.
    """
    raise NotImplementedError


def separable_conv2d(x, depthwise_kernel, pointwise_kernel, strides=(1, 1),
                     padding='valid', data_format=None, dilation_rate=(1, 1)):
    """2D convolution with separable filters.

    # Arguments
        x: input tensor
        depthwise_kernel: convolution kernel for the depthwise convolution.
        pointwise_kernel: kernel for the 1x1 convolution.
        strides: strides tuple (length 2).
        padding: string, `"same"` or `"valid"`.
        data_format: string, `"channels_last"` or `"channels_first"`.
        dilation_rate: tuple of integers,
            dilation rates for the separable convolution.

    # Returns
        Output tensor.

    # Raises
        ValueError: if `data_format` is neither `channels_last` or `channels_first`.
    """
    raise NotImplementedError


def depthwise_conv2d(x, depthwise_kernel, strides=(1, 1), padding='valid',
                     data_format=None, dilation_rate=(1, 1)):
    """2D convolution with separable filters.

    # Arguments
        x: input tensor
        depthwise_kernel: convolution kernel for the depthwise convolution.
        strides: strides tuple (length 2).
        padding: string, `"same"` or `"valid"`.
        data_format: string, `"channels_last"` or `"channels_first"`.
        dilation_rate: tuple of integers,
            dilation rates for the separable convolution.

    # Returns
        Output tensor.

    # Raises
        ValueError: if `data_format` is neither `channels_last` or `channels_first`.
    """
    raise NotImplementedError


def conv3d(x, kernel, strides=(1, 1, 1), padding='valid',
           data_format=None, dilation_rate=(1, 1, 1)):
    """3D convolution.

    # Arguments
        x: Tensor or variable.
        kernel: kernel tensor.
        strides: strides tuple.
        padding: string, `"same"` or `"valid"`.
        data_format: string, `"channels_last"` or `"channels_first"`.
            Whether to use Theano or TensorFlow/CNTK data format
            for inputs/kernels/outputs.
        dilation_rate: tuple of 3 integers.

    # Returns
        A tensor, result of 3D convolution.

    # Raises
        ValueError: if `data_format` is neither `channels_last` or `channels_first`.
    """
    if data_format is None:
        data_format = image_data_format()
    if data_format not in {'channels_first', 'channels_last'}:
        raise ValueError('Unknown data_format:', data_format)

    if hasattr(x, '_keras_shape'):
        volume_shape = _preprocess_conv3d_volume_shape(int_shape(x), data_format)
    else:
        volume_shape = None
    if hasattr(kernel, '_keras_shape'):
        kernel_shape = kernel._keras_shape
    else:
        # Will only work if `kernel` is a shared variable.
        kernel_shape = kernel.eval().shape
    kernel_shape = _preprocess_conv3d_filter_shape(kernel_shape, data_format)

    x = _preprocess_conv3d_input(x, data_format)
    kernel = _preprocess_conv3d_kernel(kernel, data_format)
    th_padding = _preprocess_padding(padding)

    conv_out = T.nnet.conv3d(x, kernel,
                             border_mode=th_padding,
                             subsample=strides,
                             input_shape=volume_shape,
                             filter_shape=kernel_shape,
                             filter_dilation=dilation_rate)
    conv_out = _postprocess_conv3d_output(conv_out, x, padding,
                                          kernel_shape, strides, data_format)
    return conv_out


def conv3d_transpose(x, kernel, output_shape, strides=(1, 1, 1),
                     padding='valid', data_format=None):
    """3D deconvolution (i.e. transposed convolution).

    # Arguments
        x: input tensor.
        kernel: kernel tensor.
        output_shape: 1D int tensor for the output shape.
        strides: strides tuple.
        padding: string, "same" or "valid".
        data_format: string, `"channels_last"` or `"channels_first"`.
            Whether to use Theano or TensorFlow/CNTK data format
            for inputs/kernels/outputs.

    # Returns
        A tensor, result of transposed 3D convolution.

    # Raises
        ValueError: if `data_format` is neither `channels_last` or `channels_first`.
    """
    flip_filters = False
    if data_format is None:
        data_format = image_data_format()
    if data_format not in {'channels_first', 'channels_last'}:
        raise ValueError('Unknown data_format ' + data_format)

    if data_format == 'channels_last':
        output_shape = (output_shape[0],
                        output_shape[4],
                        output_shape[1],
                        output_shape[2],
                        output_shape[3])

    if hasattr(kernel, '_keras_shape'):
        kernel_shape = kernel._keras_shape
    else:
        # Will only work if `kernel` is a shared variable.
        kernel_shape = kernel.eval().shape

    if padding == 'same' and kernel_shape[0] % 2 == 0:
        raise ValueError('In `Conv3DTranspose`, with padding mode `same`, '
                         'even kernel sizes are not supported with Theano. '
                         'You can set `kernel_size` to an odd number.')

    kernel_shape = _preprocess_conv3d_filter_shape(kernel_shape, data_format)

    x = _preprocess_conv3d_input(x, data_format)
    kernel = _preprocess_conv3d_kernel(kernel, data_format)

    th_padding = _preprocess_padding(padding)
    op = T.nnet.abstract_conv.AbstractConv3d_gradInputs(imshp=None,
                                                        kshp=kernel_shape,
                                                        subsample=strides,
                                                        border_mode=th_padding,
                                                        filter_flip=not flip_filters)
    conv_out = op(kernel, x, output_shape[2:])
    conv_out = _postprocess_conv3d_output(conv_out, x, padding,
                                          kernel_shape, strides, data_format)
    return conv_out


def pool2d(x, pool_size, strides=(1, 1),
           padding='valid', data_format=None,
           pool_mode='max'):
    """2D Pooling.

    # Arguments
        x: Tensor or variable.
        pool_size: tuple of 2 integers.
        strides: tuple of 2 integers.
        padding: string, `"same"` or `"valid"`.
        data_format: string, `"channels_last"` or `"channels_first"`.
        pool_mode: string, `"max"` or `"avg"`.

    # Returns
        A tensor, result of 2D pooling.

    # Raises
        ValueError: if `data_format` is neither `"channels_last"` or `"channels_first"`.
        ValueError: if `pool_mode` is neither `"max"` or `"avg"`.
    """
    if data_format is None:
        data_format = image_data_format()
    if data_format not in {'channels_first', 'channels_last'}:
        raise ValueError('Unknown data_format:', data_format)

    assert pool_size[0] >= 1 and pool_size[1] >= 1

    if padding == 'same':
        w_pad = pool_size[0] - 2 if pool_size[0] > 2 and pool_size[0] % 2 == 1 else pool_size[0] - 1
        h_pad = pool_size[1] - 2 if pool_size[1] > 2 and pool_size[1] % 2 == 1 else pool_size[1] - 1
        pad = (w_pad, h_pad)
    elif padding == 'valid':
        pad = (0, 0)
    else:
        raise ValueError('Invalid border mode:', padding)

    if data_format == 'channels_last':
        x = x.dimshuffle((0, 3, 1, 2))

    if pool_mode == 'max':
        pool_out = pool.pool_2d(x, ws=pool_size, stride=strides,
                                ignore_border=True,
                                pad=pad,
                                mode='max')
    elif pool_mode == 'avg':
        if padding == 'same':
            th_avg_pool_mode = 'average_inc_pad'
        elif padding == 'valid':
            th_avg_pool_mode = 'average_exc_pad'
        pool_out = pool.pool_2d(x, ws=pool_size, stride=strides,
                                ignore_border=True,
                                pad=pad,
                                mode=th_avg_pool_mode)
    else:
        raise ValueError('Invalid pooling mode:', pool_mode)
    if padding == 'same':
        expected_width = (x.shape[2] + strides[0] - 1) // strides[0]
        expected_height = (x.shape[3] + strides[1] - 1) // strides[1]
        pool_out = pool_out[:, :, :expected_width, :expected_height]

    if data_format == 'channels_last':
        pool_out = pool_out.dimshuffle((0, 2, 3, 1))
    return pool_out


def pool3d(x, pool_size, strides=(1, 1, 1), padding='valid',
           data_format=None, pool_mode='max'):
    """3D Pooling.

    # Arguments
        x: Tensor or variable.
        pool_size: tuple of 3 integers.
        strides: tuple of 3 integers.
        padding: string, `"same"` or `"valid"`.
        data_format: string, `"channels_last"` or `"channels_first"`.
        pool_mode: string, `"max"` or `"avg"`.

    # Returns
        A tensor, result of 3D pooling.

    # Raises
        ValueError: if `data_format` is neither `"channels_last"` or `"channels_first"`.
        ValueError: if `pool_mode` is neither `"max"` or `"avg"`.
    """
    if data_format is None:
        data_format = image_data_format()
    if data_format not in {'channels_first', 'channels_last'}:
        raise ValueError('Unknown data_format:', data_format)

    if padding == 'same':
        w_pad = pool_size[0] - 2 if pool_size[0] % 2 == 1 else pool_size[0] - 1
        h_pad = pool_size[1] - 2 if pool_size[1] % 2 == 1 else pool_size[1] - 1
        d_pad = pool_size[2] - 2 if pool_size[2] % 2 == 1 else pool_size[2] - 1
        padding = (w_pad, h_pad, d_pad)
    elif padding == 'valid':
        padding = (0, 0, 0)
    else:
        raise ValueError('Invalid padding:', padding)

    if data_format == 'channels_last':
        x = x.dimshuffle((0, 4, 1, 2, 3))

    if pool_mode == 'max':
        pool_out = pool.pool_3d(x, ws=pool_size, stride=strides,
                                ignore_border=True,
                                pad=padding,
                                mode='max')
    elif pool_mode == 'avg':
        pool_out = pool.pool_3d(x, ws=pool_size, stride=strides,
                                ignore_border=True,
                                pad=padding,
                                mode='average_exc_pad')
    else:
        raise ValueError('Invalid pooling mode:', pool_mode)

    if padding == 'same':
        expected_width = (x.shape[2] + strides[0] - 1) // strides[0]
        expected_height = (x.shape[3] + strides[1] - 1) // strides[1]
        expected_depth = (x.shape[4] + strides[2] - 1) // strides[2]

        pool_out = pool_out[:, :, :expected_width, :expected_height, :expected_depth]

    if data_format == 'channels_last':
        pool_out = pool_out.dimshuffle((0, 2, 3, 4, 1))
    return pool_out


def bias_add(x, bias, data_format=None):
    """Adds a bias vector to a tensor.

    # Arguments
        x: Tensor or variable.
        bias: Bias tensor to add.
        data_format: string, `"channels_last"` or `"channels_first"`.

    # Returns
        Output tensor.

    # Raises
        ValueError: In one of the two cases below:
                    1. invalid `data_format` argument.
                    2. invalid bias shape.
                       the bias should be either a vector or
                       a tensor with ndim(x) - 1 dimension
    """
    if data_format is None:
        data_format = image_data_format()
    if data_format not in {'channels_first', 'channels_last'}:
        raise ValueError('Unknown data_format ' + str(data_format))
    if ndim(bias) != 1 and ndim(bias) != ndim(x) - 1:
        raise ValueError('Unexpected bias dimensions %d, '
                         'expect to be 1 or %d dimensions'
                         % (ndim(bias), ndim(x) - 1))
    bias_shape = tuple(bias.shape)
    if ndim(x) == 5:
        if data_format == 'channels_first':
            if ndim(bias) == 1:
                x += reshape(bias, (1, bias_shape[0], 1, 1, 1))
            else:
                x += reshape(bias, (1, bias_shape[3]) + bias_shape[:3])
        elif data_format == 'channels_last':
            if ndim(bias) == 1:
                x += reshape(bias, (1, 1, 1, 1, bias_shape[0]))
            else:
                x += reshape(bias, (1,) + bias_shape)
    elif ndim(x) == 4:
        if data_format == 'channels_first':
            if ndim(bias) == 1:
                x += reshape(bias, (1, bias_shape[0], 1, 1))
            else:
                x += reshape(bias, (1, bias_shape[2]) + bias_shape[:2])
        elif data_format == 'channels_last':
            if ndim(bias) == 1:
                x += reshape(bias, (1, 1, 1, bias_shape[0]))
            else:
                x += reshape(bias, (1,) + bias_shape)
    elif ndim(x) == 3:
        if data_format == 'channels_first':
            if ndim(bias) == 1:
                x += reshape(bias, (1, bias_shape[0], 1))
            else:
                x += reshape(bias, (1, bias_shape[1], bias_shape[0]))
        elif data_format == 'channels_last':
            if ndim(bias) == 1:
                x += reshape(bias, (1, 1, bias_shape[0]))
            else:
                x += reshape(bias, (1,) + bias_shape)
    else:
        x += bias
    return x


# RANDOMNESS
def random_normal(shape, mean=0.0, stddev=1.0, dtype=None, seed=None):
    """Returns a tensor with normal distribution of values.

    # Arguments
        shape: A tuple of integers, the shape of tensor to create.
        mean: A float, mean of the normal distribution to draw samples.
        stddev: A float, standard deviation of the normal distribution
            to draw samples.
        dtype: String, dtype of returned tensor.
        seed: Integer, random seed.

    # Returns
        A tensor.
    """
    if dtype is None:
        dtype = floatx()
    if seed is None:
        seed = np.random.randint(1, 10e6)
    rng = RandomStreams(seed=seed)
    return rng.normal(size=shape, avg=mean, std=stddev, dtype=dtype)


def random_uniform(shape, minval=0.0, maxval=1.0, dtype=None, seed=None):
    """Returns a tensor with uniform distribution of values.

    # Arguments
        shape: A tuple of integers, the shape of tensor to create.
        minval: A float, lower boundary of the uniform distribution
            to draw samples.
        maxval: A float, upper boundary of the uniform distribution
            to draw samples.
        dtype: String, dtype of returned tensor.
        seed: Integer, random seed.

    # Returns
        A tensor.
    """
    if dtype is None:
        dtype = floatx()
    if seed is None:
        seed = np.random.randint(1, 10e6)
    rng = RandomStreams(seed=seed)
    return rng.uniform(shape, low=minval, high=maxval, dtype=dtype)


def random_binomial(shape, p=0.0, dtype=None, seed=None):
    """Returns a tensor with random binomial distribution of values.

    # Arguments
        shape: A tuple of integers, the shape of tensor to create.
        p: A float, `0. <= p <= 1`, probability of binomial distribution.
        dtype: String, dtype of returned tensor.
        seed: Integer, random seed.

    # Returns
        A tensor.
    """
    if dtype is None:
        dtype = floatx()
    if seed is None:
        seed = np.random.randint(1, 10e6)
    rng = RandomStreams(seed=seed)
    return rng.binomial(shape, p=p, dtype=dtype)


def truncated_normal(shape, mean=0.0, stddev=1.0, dtype=None, seed=None):
    """Returns a tensor with truncated random normal distribution of values.

    The generated values follow a normal distribution
    with specified mean and standard deviation,
    except that values whose magnitude is more than
    two standard deviations from the mean are dropped and re-picked.

    # Arguments
        shape: A tuple of integers, the shape of tensor to create.
        mean: Mean of the values.
        stddev: Standard deviation of the values.
        dtype: String, dtype of returned tensor.
        seed: Integer, random seed.

    # Returns
        A tensor.
    """
    if dtype is None:
        dtype = floatx()
    if seed is None:
        seed = np.random.randint(1, 10e6)
    rng = RandomStreams(seed=seed)
    normal_tensor = rng.normal(size=shape, avg=mean, std=stddev, dtype=dtype)
    # Poor man's truncated normal: we literally clip the tensor
    return T.clip(normal_tensor, mean - 2 * stddev, mean + 2 * stddev)


def random_multinomial(shape, p=0.0, dtype=None, seed=None):
    """Returns a tensor with random multinomial distribution of values.

    # Arguments
        shape: A tuple of integers, the shape of tensor to create.
        p: A float, `0. <= p <= 1`, probability of multinomial distribution.
        dtype: String, dtype of returned tensor.
        seed: Integer, random seed.

    # Returns
        A tensor.
    """
    if dtype is None:
        dtype = floatx()
    if seed is None:
        seed = np.random.randint(10e6)
    rng = RandomStreams(seed=seed)
    return rng.multinomial(shape, pvals=p, dtype=dtype)


# COUNT SKETCH
def count_sketch(h, s, x, d=16000):
    """Count sketch operator.
    See https://arxiv.org/abs/1606.01847.

    # Arguments
        h: Count sketch vector h \in \{1, d\} ^n
        s: Count sketch vector s \in \{-1, 1\} ^n
        x: Count sketch input vector
        d: Compact Bilinear dimension
    """
    rval, updates = theano.scan(fn=__count_sketch,
                                sequences=[h, s, x.dimshuffle(1, 0)],
                                outputs_info=T.alloc(0., x.shape[0], d),
                                non_sequences=[], n_steps=x.shape[1])
    return rval[-1]  # We are interested only in the last value


def __count_sketch(h, s, v,  # Sequences
                   y,  # Outputs info
                   ):
    """Count sketch utility.
    See https://arxiv.org/abs/1606.01847.

    # Arguments
        h: Count sketch vector h \in \{1, d\} ^n
        s: Count sketch vector s \in \{-1, 1\} ^n
        v: Count sketch input vector
        y: Projected output vector
    """
    return T.cast(T.inc_subtensor(y[:, h], T.dot(s, v)), 'float32')


# 1d Convolution
def scan_conv1d(u, v):
    """1D convolution over a set of vectors. All inputs will be treated by pairs.
        #x must be equal to #kernel

    # Arguments
        u: first set of vectors
        v: second set of vectors
    """

    def __vec_conv(u, v,  # Sequences
                   w,  # Outputs info
                   ):
        u = u.dimshuffle(('x', 0))
        v = v.dimshuffle(('x', 0))
        conv_out = vec_conv(u, v,
                            border_mode='full')

        init_cut = u.shape[1] / 2
        end_cut = init_cut + u.shape[1]
        return conv_out[0, init_cut:end_cut]

    conv_out, updates = theano.scan(__vec_conv,
                                    sequences=[u, v],
                                    outputs_info=T.alloc(0., u.shape[1]),  # , d),
                                    non_sequences=[], n_steps=u.shape[0])
    return conv_out


# Theano implementation of CTC
# Used with permission from Shawn Tan
# https://github.com/shawntan/
# Note that TensorFlow's native CTC code is significantly
# faster than this


def ctc_interleave_blanks(Y):
    Y_ = T.alloc(-1, Y.shape[0] * 2 + 1)
    Y_ = T.set_subtensor(Y_[T.arange(Y.shape[0]) * 2 + 1], Y)
    return Y_


def ctc_create_skip_idxs(Y):
    skip_idxs = T.arange((Y.shape[0] - 3) // 2) * 2 + 1
    non_repeats = T.neq(Y[skip_idxs], Y[skip_idxs + 2])
    return skip_idxs[non_repeats.nonzero()]


def ctc_update_log_p(skip_idxs, zeros, active, log_p_curr, log_p_prev):
    active_skip_idxs = skip_idxs[(skip_idxs < active).nonzero()]
    active_next = T.cast(T.minimum(
        T.maximum(
            active + 1,
            T.max(T.concatenate([active_skip_idxs, [-1]])) + 2 + 1
        ), log_p_curr.shape[0]), 'int32')

    common_factor = T.max(log_p_prev[:active])
    p_prev = T.exp(log_p_prev[:active] - common_factor)
    _p_prev = zeros[:active_next]
    # copy over
    _p_prev = T.set_subtensor(_p_prev[:active], p_prev)
    # previous transitions
    _p_prev = T.inc_subtensor(_p_prev[1:], _p_prev[:-1])
    # skip transitions
    _p_prev = T.inc_subtensor(_p_prev[active_skip_idxs + 2], p_prev[active_skip_idxs])
    updated_log_p_prev = T.log(_p_prev) + common_factor

    log_p_next = T.set_subtensor(
        zeros[:active_next],
        log_p_curr[:active_next] + updated_log_p_prev
    )
    return active_next, log_p_next


def ctc_path_probs(predict, Y, alpha=1e-4):
    smoothed_predict = (1 - alpha) * predict[:, Y] + alpha * np.float32(1.) / Y.shape[0]
    L = T.log(smoothed_predict)
    zeros = T.zeros_like(L[0])
    log_first = zeros

    f_skip_idxs = ctc_create_skip_idxs(Y)
    b_skip_idxs = ctc_create_skip_idxs(Y[::-1])  # there should be a shortcut to calculating this

    def step(log_f_curr, log_b_curr, f_active, log_f_prev, b_active, log_b_prev):
        f_active_next, log_f_next = ctc_update_log_p(f_skip_idxs, zeros, f_active, log_f_curr, log_f_prev)
        b_active_next, log_b_next = ctc_update_log_p(b_skip_idxs, zeros, b_active, log_b_curr, log_b_prev)
        return f_active_next, log_f_next, b_active_next, log_b_next

    [f_active, log_f_probs, b_active, log_b_probs], _ = theano.scan(
        step, sequences=[L, L[::-1, ::-1]], outputs_info=[np.int32(1), log_first, np.int32(1), log_first])

    idxs = T.arange(L.shape[1]).dimshuffle('x', 0)
    mask = (idxs < f_active.dimshuffle(0, 'x')) & (idxs < b_active.dimshuffle(0, 'x'))[::-1, ::-1]
    log_probs = log_f_probs + log_b_probs[::-1, ::-1] - L
    return log_probs, mask


def ctc_cost(predict, Y):
    log_probs, mask = ctc_path_probs(predict, ctc_interleave_blanks(Y))
    common_factor = T.max(log_probs)
    total_log_prob = T.log(T.sum(T.exp(log_probs - common_factor)[mask.nonzero()])) + common_factor
    return -total_log_prob


# batchifies original CTC code
def ctc_batch_cost(y_true, y_pred, input_length, label_length):
    """Runs CTC loss algorithm on each batch element.

    # Arguments
        y_true: tensor `(samples, max_string_length)`
            containing the truth labels.
        y_pred: tensor `(samples, time_steps, num_categories)`
            containing the prediction, or output of the softmax.
        input_length: tensor `(samples, 1)` containing the sequence length for
            each batch item in `y_pred`.
        label_length: tensor `(samples, 1)` containing the sequence length for
            each batch item in `y_true`.

    # Returns
        Tensor with shape (samples,1) containing the
            CTC loss of each element.
    """

    def ctc_step(y_true_step, y_pred_step, input_length_step, label_length_step):
        y_pred_step = y_pred_step[0: input_length_step[0]]
        y_true_step = y_true_step[0:label_length_step[0]]
        return ctc_cost(y_pred_step, y_true_step)

    ret, _ = theano.scan(
        fn=ctc_step,
        outputs_info=None,
        sequences=[y_true, y_pred, input_length, label_length]
    )

    ret = ret.dimshuffle('x', 0)
    return ret


# HIGH ORDER FUNCTIONS

def map_fn(fn, elems, name=None, dtype=None):
    """Map the function fn over the elements elems and return the outputs.

    # Arguments
        fn: Callable that will be called upon each element in elems
        elems: tensor
        name: A string name for the map node in the graph
        dtype: Output data type.

    # Returns
        Tensor with dtype `dtype`.
    """
    return theano.map(fn, elems, name=name)[0]


def foldl(fn, elems, initializer=None, name=None):
    """Reduce elems using fn to combine them from left to right.

    # Arguments
        fn: Callable that will be called upon each element in elems and an
            accumulator, for instance `lambda acc, x: acc + x`
        elems: tensor
        initializer: The first value used (`elems[0]` in case of None)
        name: A string name for the foldl node in the graph

    # Returns
        Tensor with same type and shape as `initializer`.
    """
    if initializer is None:
        initializer = elems[0]
        elems = elems[1:]

    # We need to change the order of the arguments because theano accepts x as
    # first parameter and accumulator as second
    return theano.foldl(lambda x, acc: fn(acc, x),
                        elems, initializer, name=name)[0]


def foldr(fn, elems, initializer=None, name=None):
    """Reduce elems using fn to combine them from right to left.

    # Arguments
        fn: Callable that will be called upon each element in elems and an
            accumulator, for instance `lambda acc, x: acc + x`
        elems: tensor
        initializer: The first value used (`elems[-1]` in case of None)
        name: A string name for the foldr node in the graph

    # Returns
        Tensor with same type and shape as `initializer`.
    """
    if initializer is None:
        initializer = elems[-1]
        elems = elems[:-1]

    # We need to change the order of the arguments because theano accepts x as
    # first parameter and accumulator as second
    return theano.foldr(lambda x, acc: fn(acc, x),
                        elems, initializer, name=name)[0]


def local_conv1d(inputs, kernel, kernel_size, strides, data_format=None):
    """Apply 1D conv with un-shared weights.

    # Arguments
        inputs: 3D tensor with shape: (batch_size, steps, input_dim)
        kernel: the unshared weight for convolution,
                with shape (output_length, feature_dim, filters)
        kernel_size: a tuple of a single integer,
                     specifying the length of the 1D convolution window
        strides: a tuple of a single integer,
                 specifying the stride length of the convolution
        data_format: the data format, channels_first or channels_last

    # Returns
        the tensor after 1d conv with un-shared weights, with shape (batch_size, output_length, filters)

    # Raises
        ValueError: if `data_format` is neither `channels_last` or `channels_first`.
    """
    if data_format is None:
        data_format = image_data_format()
    if data_format not in {'channels_first', 'channels_last'}:
        raise ValueError('Unknown data_format ' + str(data_format))

    stride = strides[0]
    kernel_shape = int_shape(kernel)
    output_length, feature_dim, filters = kernel_shape

    xs = []
    for i in range(output_length):
        slice_length = slice(i * stride,
                             i * stride + kernel_size[0])
        xs.append(reshape(inputs[:, slice_length, :],
                          (1, -1, feature_dim)))
    x_aggregate = concatenate(xs, axis=0)
    # Shape: `(output_length, batch_size, filters)`.
    output = batch_dot(x_aggregate, kernel)
    return permute_dimensions(output, (1, 0, 2))


def local_conv2d(inputs, kernel, kernel_size, strides, output_shape, data_format=None):
    """Apply 2D conv with un-shared weights.

    # Arguments
        inputs: 4D tensor with shape:
                (batch_size, filters, new_rows, new_cols)
                if data_format='channels_first'
                or 4D tensor with shape:
                (batch_size, new_rows, new_cols, filters)
                if data_format='channels_last'.
        kernel: the unshared weight for convolution,
                with shape (output_items, feature_dim, filters)
        kernel_size: a tuple of 2 integers, specifying the
                     width and height of the 2D convolution window.
        strides: a tuple of 2 integers, specifying the strides
                 of the convolution along the width and height.
        output_shape: a tuple with (output_row, output_col)
        data_format: the data format, channels_first or channels_last

    # Returns
        A 4d tensor with shape:
        (batch_size, filters, new_rows, new_cols)
        if data_format='channels_first'
        or 4D tensor with shape:
        (batch_size, new_rows, new_cols, filters)
        if data_format='channels_last'.

    # Raises
        ValueError: if `data_format` is neither
                    `channels_last` or `channels_first`.
    """
    if data_format is None:
        data_format = image_data_format()
    if data_format not in {'channels_first', 'channels_last'}:
        raise ValueError('Unknown data_format ' + str(data_format))

    stride_row, stride_col = strides
    output_row, output_col = output_shape
    kernel_shape = int_shape(kernel)
    _, feature_dim, filters = kernel_shape

    if data_format == 'channels_first':
        output = []
        for i in range(output_row):
            for j in range(output_col):
                slice_row = slice(i * stride_row,
                                  i * stride_row + kernel_size[0])
                slice_col = slice(j * stride_col,
                                  j * stride_col + kernel_size[1])
                x_flatten = reshape(inputs[:, :, slice_row, slice_col],
                                    (1, -1, feature_dim))
                output.append(dot(x_flatten,
                                  kernel[i * output_col + j, :, :]))
        output = concatenate(output, axis=0)
        output = reshape(output,
                         (output_row, output_col, -1, filters))
        output = permute_dimensions(output, (2, 3, 0, 1))
    else:
        xs = []
        for i in range(output_row):
            for j in range(output_col):
                slice_row = slice(i * stride_row,
                                  i * stride_row + kernel_size[0])
                slice_col = slice(j * stride_col,
                                  j * stride_col + kernel_size[1])
                xs.append(reshape(inputs[:, slice_row, slice_col, :],
                                  (1, -1, feature_dim)))

        x_aggregate = concatenate(xs, axis=0)
        output = batch_dot(x_aggregate, kernel)
        output = reshape(output,
                         (output_row, output_col, -1, filters))
        output = permute_dimensions(output, (2, 0, 1, 3))
    return output


# modified from the one included in np_utils.py
def conv_input_length(output_length, filter_size, border_mode, stride):
    if output_length is None:
        return None
    assert border_mode in {'same', 'valid', 'full'}
    add_extra = 0
    if border_mode == 'same':
        pad = filter_size // 2
        add_extra = +1
    elif border_mode == 'valid':
        pad = 0
    elif border_mode == 'full':
        pad = filter_size - 1
    return (output_length - 1) * stride - 2 * pad + filter_size + add_extra


def as_tensor_variable(x, name=None, ndim=None):
    return T.as_tensor_variable(x, name, ndim)
