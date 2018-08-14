import numpy

import chainer
from chainer import cuda
from chainer import function
from chainer import function_node
from chainer.utils import argument
from chainer.utils import type_check

if cuda.cudnn_enabled:
    cudnn = cuda.cudnn
    libcudnn = cuda.cuda.cudnn


class BatchNormalization(function_node.FunctionNode):

    mean = None
    inv_std = None

    def __init__(self, eps=2e-5, mean=None, var=None, decay=0.9,
                 active_len=0, mask=None):
        self.running_mean = mean
        self.running_var = var
        self.active_len = active_len
        self.mask = mask
        self.orig_mask = mask
        self.eps = eps
        if chainer.should_use_cudnn('>=auto'):
            if eps < 1e-5:
                msg = 'cuDNN does not allow an eps value less than 1e-5.'
                raise RuntimeError(msg)
        self.decay = decay

    def check_type_forward(self, in_types):
        type_check.expect(in_types.size() == 3)
        x_type, gamma_type, beta_type = in_types
        M = type_check.eval(gamma_type.ndim)
        type_check.expect(
            x_type.dtype.kind == 'f',
            x_type.ndim >= gamma_type.ndim + 1,
            x_type.shape[1:1 + M] == gamma_type.shape,
            # TODO(beam2d): Check shape
            gamma_type.dtype == x_type.dtype,
            beta_type.dtype == x_type.dtype,
            gamma_type.shape == beta_type.shape,
        )

    def forward(self, inputs):
        self.retain_inputs((0, 1))
        x, gamma, beta = inputs
        xp = cuda.get_array_module(x)
        ret = xp.zeros_like(x, dtype="f")
        # x = x[:self.active_len]
        if self.running_mean is None:
            self.running_mean = xp.zeros_like(gamma)
            self.running_var = xp.zeros_like(gamma)
        self.mode = _BNMode(x, gamma)

        # expander inserts singleton dimensions to gamma and beta so that they
        # can be broadcasted with x.
        head_ndim = gamma.ndim + 1
        expander = (None, Ellipsis) + (None,) * (x.ndim - head_ndim)
        self.expander = expander
        self.axis = (0,) + tuple(range(head_ndim, x.ndim))
        gamma = gamma[expander]
        beta = beta[expander]

        self.mask = xp.broadcast_to(self.mask, x.shape)
        active_x = x.transpose(0, 2, 1)
        self.mask = self.mask.transpose(0, 2, 1)
        self.mask = xp.broadcast_to(self.mask, active_x.shape)
        active_x = active_x[self.mask].reshape(-1, active_x.shape[2])
        self.mean = active_x.mean(axis=0)
        var = active_x.var(axis=0) + self.eps
        self.inv_std = var ** (-0.5)

        y = _apply_bn_fwd(xp, x, self.mean[expander],
                          self.inv_std[expander], gamma, beta)
        # Update running statistics
        m = active_x.size // gamma.size
        adjust = m / max(m - 1., 1.)  # unbiased estimation
        self.running_mean *= self.decay
        self.running_mean += (1 - self.decay) * self.mean
        self.running_var *= self.decay
        self.running_var += (1 - self.decay) * adjust * var
        y *= self.orig_mask
        # ret = y
        # ret[:self.active_len] = y
        return y,

    def backward(self, indexes, grad_outputs):
        x, gamma = self.get_retained_inputs()
        gy, = grad_outputs
        f = BatchNormalizationGrad(
            self.eps, self.mode, self.expander, self.axis,
            self.mean, self.inv_std, self.active_len,
            self.orig_mask, self.mask)
        return f(x, gamma, gy)


class BatchNormalizationGrad(function.Function):

    def __init__(self, eps, mode, expander, axis, mean, inv_std,
                 active_len, orig_mask, mask):
        self.eps = eps
        self.mode = mode
        self.expander = expander
        self.axis = axis
        self.mean = mean
        self.inv_std = inv_std
        self.active_len = active_len
        self.mask = mask
        self.orig_mask = orig_mask

    def forward(self, inputs):
        self.retain_inputs((0, 1, 2))
        x, gamma, gy = inputs
        xp = cuda.get_array_module(x)
        # ret = xp.zeros_like(x, dtype="f")
        active_gy = gy.transpose(0, 2, 1)
        active_gy = active_gy[self.mask].reshape(-1, active_gy.shape[2])
        expander = self.expander
        inv_m = gamma.dtype.type(1. / (active_gy.size // gamma.size))
        gbeta = active_gy.sum(axis=0)
        x_hat = _x_hat(x, self.mean[expander], self.inv_std[expander])
        active_x_hat = x_hat.transpose(0, 2, 1)[self.mask].reshape(-1, active_gy.shape[1])
        ggamma = (active_gy * active_x_hat).sum(axis=0)
        if xp is numpy:
            gx = (gamma * self.inv_std)[expander] * (
                gy - (x_hat * ggamma[expander] + gbeta[expander]) * inv_m)
        else:
            gx = cuda.elementwise(
                '''
                T gy, T x_hat, T gamma, T inv_std, T ggamma, T gbeta,
                T inv_m
                ''',
                'T gx',
                '''
                gx = (gamma * inv_std) * (
                    gy - (x_hat * ggamma + gbeta) * inv_m)
                ''', 'bn_bwd')(gy, x_hat, gamma[expander],
                               self.inv_std[expander], ggamma[expander],
                               gbeta[expander], inv_m)
        # ret[:self.active_len] = gx
        gx *= self.orig_mask
        self.retain_outputs((0, 1))
        return gx, ggamma, gbeta


class FixedBatchNormalization(function_node.FunctionNode):

    inv_std = None
    inv_var = None

    def __init__(self, eps=2e-5):
        self.eps = eps

    def check_type_forward(self, in_types):
        type_check.expect(in_types.size() == 5)
        x_type, gamma_type, beta_type, mean_type, var_type = in_types
        M = type_check.eval(gamma_type.ndim)
        type_check.expect(
            x_type.dtype.kind == 'f',
            x_type.ndim >= gamma_type.ndim + 1,
            x_type.shape[1:1 + M] == gamma_type.shape,
            gamma_type.dtype == x_type.dtype,
            beta_type.dtype == x_type.dtype,
            gamma_type.shape == beta_type.shape,
            mean_type.dtype == x_type.dtype,
            mean_type.shape == gamma_type.shape,
            var_type.dtype == x_type.dtype,
            var_type.shape == gamma_type.shape,
        )

    def forward(self, inputs):
        self.retain_inputs((0, 1, 3, 4))
        x, gamma, beta, mean, var = inputs
        xp = cuda.get_array_module(x)

        # expander inserts singleton dimensions to gamma and beta so that they
        # can be broadcasted with x.
        head_ndim = gamma.ndim + 1
        expander = (None, Ellipsis) + (None,) * (x.ndim - head_ndim)
        self.expander = expander
        self.axis = (0,) + tuple(range(head_ndim, x.ndim))

        mode = _BNMode(x, gamma)
        gamma = gamma[expander]
        beta = beta[expander]
        var = var + self.eps
        self.inv_var = xp.reciprocal(var)
        self.inv_std = xp.sqrt(self.inv_var, dtype=self.inv_var.dtype)
        y = _apply_bn_fwd(xp, x, mean[expander], self.inv_std[expander],
                          gamma, beta)
        return y,

    def backward(self, indexes, grad_outputs):
        x, gamma, mean, var = self.get_retained_inputs()
        gy, = grad_outputs
        f = FixedBatchNormalizationGrad(
            self.eps, self.expander, self.axis, self.inv_std, self.inv_var)
        return f(x, gamma, mean, var, gy)


class FixedBatchNormalizationGrad(function.Function):

    def __init__(self, eps, expander, axis, inv_std, inv_var):
        self.eps = eps
        self.expander = expander
        self.axis = axis
        self.inv_std = inv_std  # may be None
        self.inv_var = inv_var  # may be None

    def forward(self, inputs):
        self.retain_inputs((0, 1, 2, 4))
        x, gamma, mean, var, gy = inputs
        expander = self.expander
        xp = cuda.get_array_module(x)

        if self.inv_std is None or self.inv_var is None:
            self.inv_var = xp.reciprocal(var + self.eps)
            self.inv_std = xp.sqrt(self.inv_var, dtype=self.inv_var.dtype)

        self.gamma_over_std = gamma * self.inv_std
        x_hat = _x_hat(x, mean[expander], self.inv_std[expander])

        gx = self.gamma_over_std[expander] * gy
        gbeta = gy.sum(axis=self.axis)
        ggamma = (x_hat * gy).sum(axis=self.axis)
        gmean = -self.gamma_over_std * gbeta
        gvar = - 0.5 * gamma * self.inv_var * ggamma

        self.retain_outputs((0, 1, 2, 3, 4))
        return gx, ggamma, gbeta, gmean, gvar


class _BNMode(object):

    def __init__(self, x, gamma):
        is_gamma_1d = gamma.ndim == 1
        # cuDNN only supports these tensor dimensions because they are
        # the most commonly used. If there is a need to support other
        # dimensions with cuDNN, we could consider reshaping the input
        # into a 2-dim array with channels as second dim and m=<product
        # of all dimensions except the 2nd dimension> as the first
        # dimension.
        self.is_for_conv2d = x.ndim == 4 and is_gamma_1d
        self.is_for_linear = x.ndim == 2 and is_gamma_1d
        self.cudnn_dim_ok = self.is_for_conv2d or self.is_for_linear
        # self.cudnn_dtype_ok = x.dtype != numpy.float16
        self.cudnn_dtype_ok = self.is_for_conv2d or (x.dtype != numpy.float16)

    def get_cudnn_mode(self):
        assert self.cudnn_dim_ok
        if self.is_for_conv2d:
            return libcudnn.CUDNN_BATCHNORM_SPATIAL
        return libcudnn.CUDNN_BATCHNORM_PER_ACTIVATION

    def can_use_cudnn(self, xp):
        # TODO(bkvogel): Check for float16 support again in next cuDNN version.
        # cuDNN v5 batch normalization does not seem to support float16.
        return (xp is not numpy and
                chainer.should_use_cudnn('>=auto', 5000) and
                self.cudnn_dim_ok and
                self.cudnn_dtype_ok)


def _as4darray(arr):
    if arr.ndim == 0:
        return arr.reshape(1, 1, 1, 1)
    elif arr.ndim == 4:
        return arr
    else:
        return arr.reshape(arr.shape[0], -1, 1, 1)


def _get_mode(x, gamma):
    if x.ndim == 4 and gamma.ndim == 1:
        return libcudnn.CUDNN_BATCHNORM_SPATIAL
    return libcudnn.CUDNN_BATCHNORM_PER_ACTIVATION


def _x_hat(x, mean, inv_std):
    x_mu = x - mean
    x_mu *= inv_std
    return x_mu


def _apply_bn_fwd(xp, x, mean, inv_std, gamma, beta):
    # NOTE: all arguments should be broadcasted to x.shape
    # (mean, inv_std, gamma, and beta have to already be expanded)
    if xp is numpy:
        x_hat = _x_hat(x, mean, inv_std)
        y = gamma * x_hat
        y += beta
    else:
        y = cuda.elementwise(
            'T x, T mean, T inv_std, T gamma, T beta', 'T y',
            'y = gamma * (x - mean) * inv_std + beta', 'bn_fwd'
        )(x, mean, inv_std, gamma, beta)
    return y


def _zero_if_none(xp, x, shape, dtype):
    # TODO(Tokui): Return broadcasted 0 instead of a zeroed array.
    if x is None:
        return xp.zeros(shape, dtype=dtype)
    return x


def _get_dtype_of_tensor_descriptor(desc):
    cudnn_dtype, _, _, _, _, _, _, _, _ = libcudnn.getTensor4dDescriptor(
        desc.value)
    dtype = None
    if cudnn_dtype == libcudnn.CUDNN_DATA_DOUBLE:
        dtype = numpy.dtype(numpy.float64)
    elif cudnn_dtype == libcudnn.CUDNN_DATA_FLOAT:
        dtype = numpy.dtype(numpy.float32)
    elif cudnn_dtype == libcudnn.CUDNN_DATA_HALF:
        dtype = numpy.dtype(numpy.float16)
    else:
        msg = 'Unknow cudnn data type {} '.format(cudnn_dtype)
        raise RuntimeError(msg)
    return dtype


def batch_normalization(x, gamma, beta, **kwargs):
    """batch_normalization(x, gamma, beta, eps=2e-5, running_mean=None, running_var=None, decay=0.9)

    Batch normalization function.

    It takes the input variable ``x`` and two parameter variables ``gamma`` and
    ``beta``. The parameter variables must both have the same dimensionality,
    which is referred to as the channel shape. This channel shape corresponds
    to the dimensions in the input which are not averaged over. Since the
    first dimension of the input corresponds to the batch size, the second
    dimension of `x` will correspond to the first dimension of the channel
    shape, the third dimension of `x` will correspond to the second channel
    dimension (if it exists) and so on. Therefore, the dimensionality of the
    input must be at least one plus the number of channel dimensions. The
    total effective "batch size" will then be considered to be the product of
    all dimensions in `x` except for the channel dimensions.

    As an example, if the input is four dimensional and the parameter
    variables are one dimensional, then it is assumed that the first
    dimension of the input is the batch size, the second dimension is the
    channel size, and the remaining two dimensions are considered
    to be spatial dimensions that will be averaged over along with the
    batch size in the batch normalization computations. That is,
    the total batch size will be considered to be the product of all
    input dimensions except the second dimension.

    Note: If this function is called, it will not be possible to access the
    updated running mean and variance statistics, because they are members
    of the function object, which cannot be accessed by the caller.
    If it is desired to access the updated running statistics, it is necessary
    to get a new instance of the function object, call the object, and then
    access the running_mean and/or running_var attributes. See the
    corresponding Link class for an example of how to do this.

    .. warning::

       ``train`` argument is not supported anymore since v2.
       Instead, use ``chainer.using_config('train', train)``.
       See :func:`chainer.using_config`.

    Args:
        x (Variable): Input variable.
        gamma (Variable): Scaling parameter of normalized data.
        beta (Variable): Shifting parameter of scaled normalized data.
        eps (float): Epsilon value for numerical stability.
        running_mean (numpy.ndarray or cupy.ndarray):
            Running average of the mean. This is a
            running average of the mean over several mini-batches using
            the decay parameter. If ``None``, the running average is not
            computed. If this is ``None``, then ``runnng_var`` must also
            be ``None``.
        running_var (numpy.ndarray or cupy.ndarray):
            Running average of the variance. This is a
            running average of the variance over several mini-batches using
            the decay parameter. If ``None``, the running average is not
            computed. If this is ``None``, then ``running_mean`` must also
            be ``None``.
        decay (float): Decay rate of moving average. It is used during
            training.

    See: `Batch Normalization: Accelerating Deep Network Training by Reducing\
          Internal Covariate Shift <https://arxiv.org/abs/1502.03167>`_

    .. seealso:: :class:`links.BatchNormalization`

    """  # NOQA

    argument.check_unexpected_kwargs(
        kwargs, train='train argument is not supported anymore. '
        'Use chainer.using_config')
    eps, running_mean, running_var, decay, active_len, mask = argument.parse_kwargs(
        kwargs, ('eps', 2e-5), ('running_mean', None),
        ('running_var', None), ('decay', 0.9), ('active_len', 0), ('mask', None))

    return BatchNormalization(eps, running_mean, running_var, decay,
                              active_len, mask).apply((x, gamma, beta))[0]


def fixed_batch_normalization(x, gamma, beta, mean, var, eps=2e-5):
    """Batch normalization function with fixed statistics.

    This is a variant of batch normalization, where the mean and variance
    statistics are given by the caller as fixed variables. This is
    used on testing mode of the batch normalization layer, where batch
    statistics cannot be used for prediction consistency.

    Args:
        x (Variable): Input variable.
        gamma (Variable): Scaling parameter of normalized data.
        beta (Variable): Shifting parameter of scaled normalized data.
        mean (Variable): Shifting parameter of input.
        var (Variable): Square of scaling parameter of input.
        eps (float): Epsilon value for numerical stability.

    .. seealso::
       :func:`functions.batch_normalization`,
       :class:`links.BatchNormalization`

    """
    return FixedBatchNormalization(eps).apply((x, gamma, beta, mean, var))[0]