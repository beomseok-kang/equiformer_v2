import argparse
import datetime
import itertools
import pickle
import subprocess
import time
import torch
import numpy as np

import os
from logger import FileLogger
from pathlib import Path
import yaml
                                                                                    
from datasets.pyg.qm9 import QM9
#from torch_geometric.datasets import QM9
#from torch_geometric.nn import SchNet

from nets.equiformer_v2.equiformer_v2_qm9 import EquiformerV2_QM9

# AMP
from contextlib import suppress
from timm.utils import NativeScaler

import nets

# from timm.utils import ModelEmaV2
# from timm.scheduler import create_scheduler
from optim_factory import create_optimizer

from engine import train_one_epoch, evaluate, compute_stats

from torch.optim import AdamW
from torch.optim.lr_scheduler import LinearLR, CosineAnnealingLR, SequentialLR

# distributed training
import utils
from torch_geometric.loader import DataLoader

# ModelEma = ModelEmaV2


def get_args_parser():
    parser = argparse.ArgumentParser('Training equivariant networks', add_help=False)
    parser.add_argument('--config-file', type=str, default='qm9/U0_config.yml')
    parser.add_argument('--output-dir', type=str, default=None)
    parser.add_argument('--load-checkpoint', type=str, default=None)
    parser.add_argument('--save-checkpoint', action='store_true')
    parser.set_defaults(save_checkpoint=False)
    # network architecture
    # parser.add_argument('--model-name', type=str, default='transformer_ti')
    # parser.add_argument('--input-irreps', type=str, default=None)
    # parser.add_argument('--radius', type=float, default=2.0)
    # parser.add_argument('--num-basis', type=int, default=32)
    # parser.add_argument('--output-channels', type=int, default=1)
    # training hyper-parameters
    # parser.add_argument("--epochs", type=int, default=300)
    # parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument('--model-ema', action='store_true')
    parser.add_argument('--no-model-ema', action='store_false', dest='model_ema')
    parser.set_defaults(model_ema=True)
    parser.add_argument('--model-ema-decay', type=float, default=0.9999, help='')
    parser.add_argument('--model-ema-force-cpu', action='store_true', default=False, help='')
    # regularization
    parser.add_argument('--drop-path', type=float, default=0.0)
    # optimizer (timm)
    # parser.add_argument('--opt', default='adamw', type=str, metavar='OPTIMIZER',
    #                     help='Optimizer (default: "adamw"')
    # parser.add_argument('--opt-eps', default=1e-8, type=float, metavar='EPSILON',
    #                     help='Optimizer Epsilon (default: 1e-8)')
    # parser.add_argument('--opt-betas', default=None, type=float, nargs='+', metavar='BETA',
    #                     help='Optimizer Betas (default: None, use opt default)')
    # parser.add_argument('--clip-grad', type=float, default=None, metavar='NORM',
    #                     help='Clip gradient norm (default: None, no clipping)')
    # parser.add_argument('--momentum', type=float, default=0.9, metavar='M',
    #                     help='SGD momentum (default: 0.9)')
    # parser.add_argument('--weight-decay', type=float, default=0.01,
    #                     help='weight decay (default: 0.01)')
    # learning rate schedule parameters (timm)
    # parser.add_argument('--sched', default='cosine', type=str, metavar='SCHEDULER',
    #                     help='LR scheduler (default: "cosine"')
    # parser.add_argument('--lr', type=float, default=5e-4, metavar='LR',
    #                     help='learning rate (default: 5e-4)')
    # parser.add_argument('--lr-noise', type=float, nargs='+', default=None, metavar='pct, pct',
    #                     help='learning rate noise on/off epoch percentages')
    # parser.add_argument('--lr-noise-pct', type=float, default=0.67, metavar='PERCENT',
    #                     help='learning rate noise limit percent (default: 0.67)')
    # parser.add_argument('--lr-noise-std', type=float, default=1.0, metavar='STDDEV',
    #                     help='learning rate noise std-dev (default: 1.0)')
    # parser.add_argument('--warmup-lr', type=float, default=1e-6, metavar='LR',
    #                     help='warmup learning rate (default: 1e-6)')
    # parser.add_argument('--min-lr', type=float, default=1e-5, metavar='LR',
    #                     help='lower lr bound for cyclic schedulers that hit 0 (1e-5)')

    # parser.add_argument('--decay-epochs', type=float, default=30, metavar='N',
    #                     help='epoch interval to decay LR')
    # parser.add_argument('--warmup-epochs', type=int, default=5, metavar='N',
    #                     help='epochs to warmup LR, if scheduler supports')
    # parser.add_argument('--cooldown-epochs', type=int, default=10, metavar='N',
    #                     help='epochs to cooldown LR at min_lr, after cyclic schedule ends')
    # parser.add_argument('--patience-epochs', type=int, default=10, metavar='N',
    #                     help='patience epochs for Plateau LR scheduler (default: 10')
    # parser.add_argument('--decay-rate', '--dr', type=float, default=0.1, metavar='RATE',
    #                     help='LR decay rate (default: 0.1)')
    # logging
    parser.add_argument("--print-freq", type=int, default=100)
    # task
    parser.add_argument("--target", type=int, default=7)
    parser.add_argument("--data-path", type=str, default='data/qm9')
    parser.add_argument('--feature-type', type=str, default='one_hot')
    parser.add_argument('--compute-stats', action='store_true', dest='compute_stats')
    parser.set_defaults(compute_stats=False)
    parser.add_argument('--no-standardize', action='store_false', dest='standardize')
    parser.set_defaults(standardize=True)
    parser.add_argument('--loss', type=str, default='l1')
    # random
    parser.add_argument("--seed", type=int, default=0)
    # data loader config
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument('--pin-mem', action='store_true',
                        help='Pin CPU memory in DataLoader for more efficient (sometimes) transfer to GPU.')
    parser.add_argument('--no-pin-mem', action='store_false', dest='pin_mem',
                        help='')
    parser.set_defaults(pin_mem=True)
    # AMP
    parser.add_argument('--no-amp', action='store_false', dest='amp', 
                        help='Disable FP16 training.')
    parser.set_defaults(amp=True)
    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--dist_url', default='env://', help='url used to set up distributed training')
    return parser

def main(args, config_yml):
    
    # TODO: fix distributed training

    # utils.init_distributed_mode(args)
    # is_main_process = (args.rank == 0)
    is_main_process = True
    args.distributed = False
    args.rank = 0
    args.local_rank = 0
    
    _log = FileLogger(is_master=is_main_process, is_rank0=is_main_process, output_dir=args.output_dir)
    _log.info(args)
    
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    
    ''' Dataset '''
    train_dataset = QM9(args.data_path, 'train', feature_type=args.feature_type)
    val_dataset   = QM9(args.data_path, 'valid', feature_type=args.feature_type)
    test_dataset  = QM9(args.data_path, 'test', feature_type=args.feature_type)
    _log.info('Training set mean: {}, std:{}'.format(
        train_dataset.mean(args.target), train_dataset.std(args.target)))
    # calculate dataset stats
    task_mean, task_std = 0, 1
    if args.standardize:
        task_mean, task_std = train_dataset.mean(args.target), train_dataset.std(args.target)
    norm_factor = [task_mean, task_std]
    
    # since dataset needs random 
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    
    ''' Network '''
    
    hyperparams = config_yml["model"]
    model = EquiformerV2_QM9(
        None, None, None, # not used
        use_pbc=hyperparams["use_pbc"],
        regress_forces=hyperparams["regress_forces"],
        otf_graph=hyperparams["otf_graph"],
        max_neighbors=hyperparams["max_neighbors"],
        max_radius=hyperparams["max_radius"],
        max_num_elements=hyperparams["max_num_elements"],
        
        num_layers=hyperparams["num_layers"],
        sphere_channels=hyperparams["sphere_channels"],
        attn_hidden_channels=hyperparams["attn_hidden_channels"],
        num_heads=hyperparams["num_heads"],
        attn_alpha_channels=hyperparams["attn_alpha_channels"],
        attn_value_channels=hyperparams["attn_value_channels"],
        ffn_hidden_channels=hyperparams["ffn_hidden_channels"],
        
        norm_type=hyperparams["norm_type"],
        
        lmax_list=hyperparams["lmax_list"],
        mmax_list=hyperparams["mmax_list"],
        grid_resolution=hyperparams["grid_resolution"],
        
        num_sphere_samples=hyperparams["num_sphere_samples"],
        
        edge_channels=hyperparams["edge_channels"],
        use_atom_edge_embedding=hyperparams["use_atom_edge_embedding"],
        share_atom_edge_embedding=hyperparams["share_atom_edge_embedding"],
        distance_function=hyperparams["distance_function"],
        num_distance_basis=hyperparams["num_distance_basis"],
        
        attn_activation=hyperparams["attn_activation"],
        use_s2_act_attn=hyperparams["use_s2_act_attn"],
        use_attn_renorm=hyperparams["use_attn_renorm"],
        ffn_activation=hyperparams["ffn_activation"],
        use_gate_act=hyperparams["use_gate_act"],
        use_grid_mlp=hyperparams["use_grid_mlp"],
        use_sep_s2_act=hyperparams["use_sep_s2_act"],
        
        alpha_drop=hyperparams["alpha_drop"],
        drop_path_rate=hyperparams["drop_path_rate"],
        proj_drop=hyperparams["proj_drop"],
        weight_init=hyperparams["weight_init"],
    )
    _log.info(model)
    model = model.to(device)
    
    model_ema = None
    # if args.model_ema:
    #     # Important to create EMA model after cuda(), DP wrapper, and AMP but before SyncBN and DDP wrapper
    #     model_ema = ModelEma(
    #         model,
    #         decay=args.model_ema_decay,
    #         device='cpu' if args.model_ema_force_cpu else None)

    # distributed training
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank])

    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    _log.info('Number of params: {}'.format(n_parameters))
    
    ''' Optimizer and LR Scheduler '''
    optim_params = config_yml["optim"]
    # TODO: If weight decay needed, need to add it to the optimizer
    if optim_params["optimizer"] == "AdamW":
        optimizer = AdamW(
            model.parameters(),
            lr=optim_params["lr_initial"],
            weight_decay=optim_params["optimizer_params"]["weight_decay"]
        )
    else:
        raise ValueError(f"Optimizer {optim_params['optimizer']} not supported")
    
    if optim_params["scheduler"] == "linear_warmup_cosine_annealing":
        scheduler_params = optim_params["scheduler_params"]
        warmup_linear_scheduler = LinearLR(
            optimizer,
            start_factor=scheduler_params["warmup_factor"],
            total_iters=scheduler_params["warmup_epochs"],
        )
        cos_scheduler = CosineAnnealingLR(
            optimizer,
            optim_params["max_epochs"] - scheduler_params["warmup_epochs"],
            eta_min=scheduler_params["lr_min_factor"] * scheduler_params["lr_min_factor"],
        )
        lr_scheduler = SequentialLR(
            optimizer,
            schedulers=[warmup_linear_scheduler, cos_scheduler],
            milestones=[scheduler_params["warmup_epochs"]],
        )
    else:
        raise ValueError(f"Scheduler {optim_params['scheduler']} not supported")
    
    # TODO: Add force loss
    if optim_params["loss_energy"] == "mae":
        criterion = torch.nn.L1Loss()
    elif optim_params["loss_energy"] == "mse":
        criterion = torch.nn.MSELoss()
    else:
        raise ValueError(f"Loss {optim_params['loss']} not supported")

    ''' AMP (from timm) '''
    # setup automatic mixed-precision (AMP) loss scaling and op casting
    amp_autocast = suppress  # do nothing
    loss_scaler = None
    if args.amp:
        amp_autocast = torch.cuda.amp.autocast
        loss_scaler = NativeScaler()
    
    ''' Data Loader '''
    if args.distributed:
        sampler_train = torch.utils.data.DistributedSampler(
                train_dataset, num_replicas=utils.get_world_size(), rank=utils.get_rank(), shuffle=True
            )
        train_loader = DataLoader(train_dataset, batch_size=args.batch_size, 
            sampler=sampler_train, num_workers=args.workers, pin_memory=args.pin_mem, 
            drop_last=True)
    else:
        # train_loader = DataLoader(train_dataset, batch_size=args.batch_size, 
        #     shuffle=True, num_workers=args.workers, pin_memory=args.pin_mem, 
        #     drop_last=True)
        train_loader = DataLoader(
            train_dataset,
            batch_size=optim_params["batch_size"], 
            shuffle=True,
            num_workers=optim_params["num_workers"],
            pin_memory=args.pin_mem, 
            drop_last=True
        )
    val_loader = DataLoader(val_dataset, batch_size=optim_params["batch_size"])
    test_loader = DataLoader(test_dataset, batch_size=optim_params["batch_size"])
    
    ''' Compute stats '''
    if args.compute_stats:
        compute_stats(train_loader, max_radius=args.radius, logger=_log, print_freq=args.print_freq)
        return
    
    best_epoch, best_train_err, best_val_err, best_test_err = 0, float('inf'), float('inf'), float('inf')
    best_ema_epoch, best_ema_val_err, best_ema_test_err = 0, float('inf'), float('inf')
    
    if args.load_checkpoint is not None:
        checkpoint = torch.load(args.load_checkpoint)
        model.cpu().load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        lr_scheduler.load_state_dict(checkpoint['scheduler_state_dict'])
        start_epoch = checkpoint['epoch'] + 1
        model = model.to(device)
    else:
        start_epoch = 0
    
    for epoch in range(start_epoch, optim_params["max_epochs"]):
        
        epoch_start_time = time.perf_counter()
        
        lr_scheduler.step()

        if args.distributed:
            train_loader.sampler.set_epoch(epoch)
        
        train_err = train_one_epoch(model=model, criterion=criterion, norm_factor=norm_factor,
            target=args.target, data_loader=train_loader, optimizer=optimizer,
            device=device, epoch=epoch, model_ema=model_ema, 
            amp_autocast=amp_autocast, loss_scaler=loss_scaler,
            print_freq=args.print_freq, logger=_log)
        
        val_err, val_loss = evaluate(model, norm_factor, args.target, val_loader, device, 
            amp_autocast=amp_autocast, print_freq=args.print_freq, logger=_log)
        
        test_err, test_loss = evaluate(model, norm_factor, args.target, test_loader, device, 
            amp_autocast=amp_autocast, print_freq=args.print_freq, logger=_log)
        
        # record the best results
        if val_err < best_val_err:
            best_val_err = val_err
            best_test_err = test_err
            best_train_err = train_err
            best_epoch = epoch

        info_str = 'Epoch: [{epoch}] Target: [{target}] train MAE: {train_mae:.5f}, '.format(
            epoch=epoch, target=args.target, train_mae=train_err)
        info_str += 'val MAE: {:.5f}, '.format(val_err)
        info_str += 'test MAE: {:.5f}, '.format(test_err)
        info_str += 'Time: {:.2f}s'.format(time.perf_counter() - epoch_start_time)
        _log.info(info_str)
        
        info_str = 'Best -- epoch={}, train MAE: {:.5f}, val MAE: {:.5f}, test MAE: {:.5f}\n'.format(
            best_epoch, best_train_err, best_val_err, best_test_err)
        _log.info(info_str)
        
        torch.save(
            {
                'model_state_dict': model.cpu().state_dict(), 
                'optimizer_state_dict': optimizer.state_dict(), 
                'scheduler_state_dict': lr_scheduler.state_dict(),
                'epoch': epoch, # should start from the next epoch
            }, 
            f'{args.output_dir}/checkpoint.pth'
        )
        model = model.to(device)
        
        # evaluation with EMA
        if model_ema is not None:
            ema_val_err, _ = evaluate(model_ema.module, norm_factor, args.target, val_loader, device, 
                amp_autocast=amp_autocast, print_freq=args.print_freq, logger=_log)
            
            ema_test_err, _ = evaluate(model_ema.module, norm_factor, args.target, test_loader, device, 
                amp_autocast=amp_autocast, print_freq=args.print_freq, logger=_log)
            
            # record the best results
            if (ema_val_err) < best_ema_val_err:
                best_ema_val_err = ema_val_err
                best_ema_test_err = ema_test_err
                best_ema_epoch = epoch
    
            info_str = 'Epoch: [{epoch}] Target: [{target}] '.format(
                epoch=epoch, target=args.target)
            info_str += 'EMA val MAE: {:.5f}, '.format(ema_val_err)
            info_str += 'EMA test MAE: {:.5f}, '.format(ema_test_err)
            info_str += 'Time: {:.2f}s'.format(time.perf_counter() - epoch_start_time)
            _log.info(info_str)
            
            info_str = 'Best EMA -- epoch={}, val MAE: {:.5f}, test MAE: {:.5f}\n'.format(
                best_ema_epoch, best_ema_val_err, best_ema_test_err)
            _log.info(info_str)
        
        

if __name__ == "__main__":
    
    parser = argparse.ArgumentParser('Training equivariant networks', parents=[get_args_parser()])
    args = parser.parse_args()
    config_yml = yaml.safe_load(open(args.config_file, "r"))
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args, config_yml)
    
