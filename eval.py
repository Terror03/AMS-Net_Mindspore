# Copyright 2021 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Eval"""
import os
import time
from mindspore.context import set_context, set_auto_parallel_context, reset_auto_parallel_context, \
    ParallelMode, PYNATIVE_MODE
from mindspore.train import Model
from mindspore.communication import init
from mindspore import load_checkpoint, load_param_into_net
from src.utils.dataset import get_datasets
from src.utils.temporal_shift import make_temporal_pool
from src.utils.dataset_config import return_dataset
from src.model.net import TSM
from src.model.cross_entropy_smooth import CrossEntropySmooth
from src.model_utils.config import config
from src.model_utils.device_adapter import get_device_id, get_device_num, get_rank_id

def main():
    args = config
    modelarts_init(args)
    num_class, args.train_list, args.val_list, args.root_path, prefix = return_dataset(args.dataset,
                                                                                       args.modality, args.data_path)
    store_name(args)
    base_model = args.arch

    if get_device_num() > 1:
        set_context(mode=PYNATIVE_MODE, device_target=args.device_target, device_id=get_device_id())
        reset_auto_parallel_context()
        set_auto_parallel_context(parallel_mode=ParallelMode.DATA_PARALLEL,
                                  gradients_mean=True, device_num=get_device_num())
        init()
        rank = get_rank_id()
        args.gpus = get_device_num()

    elif get_device_num() == 1:
        set_context(mode=PYNATIVE_MODE, device_target=args.device_target)
        reset_auto_parallel_context()
        rank = get_rank_id()
        args.gpus = get_device_num()
        set_auto_parallel_context(parallel_mode=ParallelMode.STAND_ALONE, device_num=get_device_num())


    check_rootfolders(args)
    net = TSM(num_class, args.num_segments, args.modality,
              base_model=base_model,
              consensus_type=args.consensus_type,
              dropout=args.dropout,
              img_feature_dim=args.img_feature_dim,
              partial_bn=not args.no_partialbn,
              pretrain=args.pretrain,
              is_shift=args.shift, shift_div=args.shift_div, shift_place=args.shift_place,
              fc_lr5=not (args.tune_from and args.dataset in args.tune_from),
              temporal_pool=args.temporal_pool,
              non_local=args.non_local)

    criterion = CrossEntropySmooth(True, 'mean', num_classes=num_class)
    crop_size = net.crop_size
    scale_size = net.scale_size
    input_mean = net.input_mean
    input_std = net.input_std
    policies = net.get_optim_policies()
    train_augmentation = net.get_augmentation(flip=False)
    if args.temporal_pool and not args.resume:
        make_temporal_pool(net.module.base_model, args.num_segments)

    _, val_loader = get_datasets(args, rank, input_mean, train_augmentation,
                                 input_std, scale_size, crop_size, prefix)

    for group in policies:
        print(('group: {} has {} params, lr_mult: {}, decay_mult: {}'.format(
            group['name'], len(group['params']), group['lr_mult'], group['decay_mult'])))


    if args.no_partialbn:
        net.partialBN(False)
    else:
        net.partialBN(True)

    # switch to train mode
    net.set_train(False)
    check_point = os.path.join(args.checkpoint_path, args.test_filename)
    param_dict = load_checkpoint(check_point)
    #for name in param_dict.keys():
        #print(name)
    load_param_into_net(net, param_dict)

    model = Model(net, loss_fn=criterion, metrics={'top_1_accuracy', 'top_5_accuracy'})


    # eval model
    res = model.eval(val_loader)
    print("result:", res, "ckpt=", args.test_filename)


def check_rootfolders(args):
    """Create log and model folder"""
    folders_util = [args.root_log, args.root_model,
                    os.path.join(args.root_log, args.store_name),
                    os.path.join(args.root_model, args.store_name)]
    for folder in folders_util:
        if not os.path.exists(folder):
            print('creating folder ' + folder)
            os.mkdir(folder)

def modelarts_init(args):
    """modelarts_init"""
    if args.enable_modelarts:
        import moxing as mox

        notice_tar_over = os.path.join(args.data_path, 'zip_is_over')
        if get_rank_id() == 0:
            mox.file.copy_parallel(src_url=config.data_url, dst_url=args.data_path)
            zip_command = f"tar -xf {args.data_path}/somethingv2.tar -C {args.data_path}"
            print(zip_command)

            os.system(zip_command)
            os.mkdir(notice_tar_over)

        while True:
            if os.path.exists(notice_tar_over):
                break
            time.sleep(1)

        print("Unzip success!")

def store_name(args):
    """store_name"""
    full_arch_name = args.arch
    if args.shift:
        full_arch_name += '_shift{}_{}'.format(args.shift_div, args.shift_place)
    if args.temporal_pool:
        full_arch_name += '_tpool'
    args.store_name = '_'.join(
        ['TSM', args.dataset, args.modality, full_arch_name, args.consensus_type, 'segment%d' % args.num_segments,
         'e{}'.format(args.epochs)])
    if args.pretrain != 'imagenet':
        args.store_name += '_{}'.format(args.pretrain)
    if args.lr_type != 'step':
        args.store_name += '_{}'.format(args.lr_type)
    if args.dense_sample:
        args.store_name += '_dense'
    if args.non_local > 0:
        args.store_name += '_nl'
    if args.suffix is not None:
        args.store_name += '_{}'.format(args.suffix)
    print('storing name: ' + args.store_name)


if __name__ == '__main__':
    main()
