## @package sparse_lookup
# Module caffe2.python.layers.sparse_lookup
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from caffe2.python.helpers.arg_scope import get_current_scope
from caffe2.python import schema
from caffe2.python.layers.layers import (
    get_categorical_limit,
    IdList,
    IdScoreList,
    LayerPsParam,
    ModelLayer,
)
import collections
import functools
import math
import numpy as np
import operator


def get_sparse_lookup_predictor_version(version):
    assert version in ('fp16', 'uint8rowwise'),\
        "Unexpected version of sparse_lookup layer {0}".format(version)
    return version


class SparseLookup(ModelLayer):
    _id_list_supported_reducers = ['PositionWeighted', 'LogMeanExp', 'LogSumExp',
                                   'Max', 'Mean', 'Sum', 'Sqrt', 'None']

    _id_score_list_supported_reducers = ['PositionWeighted', 'Mean', 'Sum',
                                         'WeightedSum', 'WeightedMean', 'None']

    def __init__(self, model, input_record, inner_shape, reducer,
                 weight_init=None, weight_optim=None,
                 name='sparse_lookup', **kwargs):

        super(SparseLookup, self).__init__(model, name, input_record, **kwargs)

        # TODO Add some asserts about input type
        if isinstance(inner_shape, int):
            inner_shape = [inner_shape]
        assert isinstance(inner_shape, list) or isinstance(inner_shape, tuple),\
            "Unexpected type for inner_shape, expected list or tuple, got {0}".\
            format(type(inner_shape))

        if reducer == "PositionWeighted":
            self.external_weights = input_record.values()
        self.reducer = reducer

        input_dim = get_categorical_limit(input_record)
        assert input_dim is not None, "Unbounded features are not supported"

        scale = math.sqrt(1.0 / input_dim)
        self.shape = [input_dim] + inner_shape
        self.weight_init = weight_init if weight_init else (
            'UniformFill', {'min': -scale, 'max': scale})

        if schema.equal_schemas(self.input_record, IdList):
            sparse_key = self.input_record.items()
        elif schema.equal_schemas(
                self.input_record,
                IdScoreList,
                check_field_types=False):
            sparse_key = self.input_record.keys()
        else:
            raise NotImplementedError()

        if self.input_record.lengths.metadata:
            avg_length = self.input_record.lengths.metadata.expected_value
        else:
            avg_length = None

        self.w = self.create_param(
            param_name='w',
            shape=self.shape,
            initializer=self.weight_init,
            optimizer=weight_optim,
            ps_param=LayerPsParam(
                sparse_key=sparse_key,
                average_length=avg_length))

        self.scale_bias_init = ('ConstantFill', {'value': 0.0})

        self.scale_bias = self.create_param(
            param_name='scale_bias',
            shape=[],
            initializer=self.scale_bias_init,
            optimizer=model.NoOptim)



        self.output_schema = schema.Scalar(
            (np.float32, inner_shape),
            self.get_next_blob_reference('output'),
        )

    def get_memory_usage(self):
        return functools.reduce(operator.mul, self.shape) * 4

    def get_fp16_compatible_parameters(self):
        return [self.w]

    def get_8bits_compatible_parameters(self):
        RowwiseQuantized8BitsWeight =\
            collections.namedtuple(
                'RowwiseQuantized8BitsWeight',
                ['w', 'scale_bias'], verbose=True)

        weight = RowwiseQuantized8BitsWeight(
            self.w, self.scale_bias)
        return [weight]

    def _gather_wrapper(self, net, version, in_indices, out):
        if version == 'fp16':
            return net.Gather([self.w, in_indices], out, engine='fp16')

        elif version == 'uint8rowwise':
            gathered_w = net.Gather([self.w, in_indices],
                                    engine='fp16')
            gathered_scale_bias = net.Gather(
                [self.scale_bias, in_indices],
                engine='fp16')

            return net.Rowwise8BitQuantizedToFloat(
                [gathered_w, gathered_scale_bias], out)
        else:
            raise "Unsupported version of operators in SparseLookup " +\
                "layer: {0}".format(version)

    def _sparse_lengths_weighted_reducer(
            self, in_indices, weights, reducer,
            net, version, grad_on_weights=0):
        op_input = [self.w,
                    weights,
                    in_indices,
                    self.input_record.lengths()]
        layer_name = 'SparseLengths' + reducer
        if version == 'fp16':
            net.__getattr__(layer_name)(
                op_input,
                self.output_schema.field_blobs(),
                grad_on_weights=grad_on_weights,
                engine='fp16',
            )
        elif version == 'uint8rowwise':
            op_input.insert(len(op_input), self.scale_bias)
            net.__getattr__(layer_name + '8BitsRowwise')(
                op_input, self.output_schema.field_blobs())
        else:
            raise "Unsupported version of operator in SparseLookUp " +\
                "layer: {0}".format(version)



    # deal with sparse features of id_list type
    def _add_ops_id_list(self, net, version='fp16'):
        assert self.reducer in self._id_list_supported_reducers, (
            "Unsupported reducer: {} for ID_LIST".format(self.reducer)
        )
        if self.reducer in ['Sum', 'Mean']:
            op_input = [self.w,
                        self.input_record.items(),
                        self.input_record.lengths()]

            layer_name = 'SparseLengths' + self.reducer
            if version == 'fp16':
                net.__getattr__(layer_name)(
                    op_input,
                    self.output_schema.field_blobs(),
                    engine='fp16',
                )
            elif version == 'uint8rowwise':
                op_input.insert(len(op_input), self.scale_bias)
                net.__getattr__(layer_name + '8BitsRowwise')(
                    op_input, self.output_schema.field_blobs())
            else:
                raise "Unsupported version of operator in SparseLookUp " +\
                    "layer: {0}".format(version)

        elif self.reducer == 'Sqrt':
            sqrt_weight = net.LengthsToWeights(
                [self.input_record.lengths()],
                [self.input_record.lengths() + '_sqrt'],
                power=0.5,
            )
            self._sparse_lengths_weighted_reducer(
                self.input_record.items(),
                sqrt_weight,
                'WeightedSum', net, version)

        elif self.reducer == 'None':
            # Gather operator will gather the embedding for each id of
            # each IdList.
            self._gather_wrapper(net, version, self.input_record.items(),
                                 self.output_schema.field_blobs())

        else:
            table_rows = self._gather_wrapper(
                net, version, self.input_record.items(), 1)

            segment_ids = net.LengthsToSegmentIds(
                self.input_record.lengths(),
                self.input_record.lengths() + '_sid'),
            net.__getattr__('SortedSegmentRange' + self.reducer)(
                [table_rows, segment_ids],
                self.output_schema.field_blobs(),
                engine='fp16',
            )


    # deal with sparse features of id_score_list type
    def _add_ops_id_score_list(self, net, version='fp16'):
        assert self.reducer in self._id_score_list_supported_reducers, (
            "Unsupported reducer: {} for ID_SCORE_LIST".format(self.reducer)
        )
        if self.reducer in ['WeightedSum', 'WeightedMean']:
            self._sparse_lengths_weighted_reducer(
                self.input_record.keys(),
                self.input_record.values(),
                self.reducer, net, version)

        elif self.reducer in ['Sum', 'Mean']:
            op_input = [self.w,
                        self.input_record.keys(),
                        self.input_record.lengths()]

            layer_name = 'SparseLengths' + self.reducer
            if version == 'fp16':
                net.__getattr__(layer_name)(
                    op_input,
                    self.output_schema.field_blobs(),
                    engine='fp16',
                )
            elif version == 'uint8rowwise':
                net.__getattr__(layer_name + '8BitsRowwise')(
                    op_input, self.output_schema.field_blobs())
            else:
                raise "Unsupported version of operator in SparseLookUp " +\
                    "layer: {0}".format(version)


        elif self.reducer == 'PositionWeighted':
            self._sparse_lengths_weighted_reducer(
                self.input_record.keys(),
                self.external_weights,
                'WeightedSum', net, version, grad_on_weights=1)

        elif self.reducer == 'None':
            # Gather operator will gather the embedding for each id of
            # each IdList.
            self._gather_wrapper(net, version, self.input_record.keys(),
                                 self.output_schema.field_blobs())
        else:
            raise "Only Sum, Mean, None are supported for IdScoreList input." +\
                "Trying to create with {}".format(self.reducer)

    def add_ops(self, net):
        cur_scope = get_current_scope()
        version = get_sparse_lookup_predictor_version(
            **cur_scope.get(get_sparse_lookup_predictor_version.__name__,
                            {'version': 'fp16'}))

        if schema.equal_schemas(self.input_record, IdList):
            self._add_ops_id_list(net, version=version)
        elif schema.equal_schemas(self.input_record,
                                  IdScoreList,
                                  check_field_types=False):
            self._add_ops_id_score_list(net, version=version)
        else:
            raise "Unsupported input type {0}".format(self.input_record)