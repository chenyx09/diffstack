"""
This file contains some PyTorch utilities.
"""
import numpy as np
import pytorch_lightning as pl
import torch
import torch.optim as optim
import functools
from tqdm.auto import tqdm
import time
from typing import Optional, Union


def soft_update(source, target, tau):
    """
    Soft update from the parameters of a @source torch module to a @target torch module
    with strength @tau. The update follows target = target * (1 - tau) + source * tau.

    Args:
        source (torch.nn.Module): source network to push target network parameters towards
        target (torch.nn.Module): target network to update
    """
    for target_param, param in zip(target.parameters(), source.parameters()):
        target_param.copy_(target_param * (1.0 - tau) + param * tau)


def hard_update(source, target):
    """
    Hard update @target parameters to match @source.

    Args:
        source (torch.nn.Module): source network to provide parameters
        target (torch.nn.Module): target network to update parameters for
    """
    for target_param, param in zip(target.parameters(), source.parameters()):
        target_param.copy_(param)


def get_torch_device(try_to_use_cuda):
    """
    Return torch device. If using cuda (GPU), will also set cudnn.benchmark to True
    to optimize CNNs.

    Args:
        try_to_use_cuda (bool): if True and cuda is available, will use GPU

    Returns:
        device (torch.Device): device to use for models
    """
    if try_to_use_cuda and torch.cuda.is_available():
        torch.backends.cudnn.benchmark = True
        device = torch.device("cuda:0")
    else:
        device = torch.device("cpu")
    return device


def reparameterize(mu, logvar):
    """
    Reparameterize for the backpropagation of z instead of q.
    This makes it so that we can backpropagate through the sampling of z from
    our encoder when feeding the sampled variable to the decoder.

    (See "The reparameterization trick" section of https://arxiv.org/abs/1312.6114)

    Args:
        mu (torch.Tensor): batch of means from the encoder distribution
        logvar (torch.Tensor): batch of log variances from the encoder distribution

    Returns:
        z (torch.Tensor): batch of sampled latents from the encoder distribution that
            support backpropagation
    """
    # logvar = \log(\sigma^2) = 2 * \log(\sigma)
    # \sigma = \exp(0.5 * logvar)

    # clamped for numerical stability
    logstd = (0.5 * logvar).clamp(-4, 15)
    std = torch.exp(logstd)

    # Sample \epsilon from normal distribution
    # use std to create a new tensor, so we don't have to care
    # about running on GPU or not
    eps = std.new(std.size()).normal_()

    # Then multiply with the standard deviation and add the mean
    z = eps.mul(std).add_(mu)

    return z


def optimizer_from_optim_params(net_optim_params, net):
    """
    Helper function to return a torch Optimizer from the optim_params
    section of the config for a particular network.

    Args:
        optim_params (Config): optim_params part of algo_config corresponding
            to @net. This determines the optimizer that is created.

        net (torch.nn.Module): module whose parameters this optimizer will be
            responsible

    Returns:
        optimizer (torch.optim.Optimizer): optimizer
    """
    return optim.Adam(
        params=net.parameters(),
        lr=net_optim_params["learning_rate"]["initial"],
        weight_decay=net_optim_params["regularization"]["L2"],
    )


def lr_scheduler_from_optim_params(net_optim_params, net, optimizer):
    """
    Helper function to return a LRScheduler from the optim_params
    section of the config for a particular network. Returns None
    if a scheduler is not needed.

    Args:
        optim_params (Config): optim_params part of algo_config corresponding
            to @net. This determines whether a learning rate scheduler is created.

        net (torch.nn.Module): module whose parameters this optimizer will be
            responsible

        optimizer (torch.optim.Optimizer): optimizer for this net

    Returns:
        lr_scheduler (torch.optim.lr_scheduler or None): learning rate scheduler
    """
    lr_scheduler = None
    if len(net_optim_params["learning_rate"]["epoch_schedule"]) > 0:
        # decay LR according to the epoch schedule
        lr_scheduler = optim.lr_scheduler.MultiStepLR(
            optimizer=optimizer,
            milestones=net_optim_params["learning_rate"]["epoch_schedule"],
            gamma=net_optim_params["learning_rate"]["decay_factor"],
        )
    return lr_scheduler


def backprop_for_loss(net, optim, loss, max_grad_norm=None, retain_graph=False):
    """
    Backpropagate loss and update parameters for network with
    name @name.

    Args:
        net (torch.nn.Module): network to update

        optim (torch.optim.Optimizer): optimizer to use

        loss (torch.Tensor): loss to use for backpropagation

        max_grad_norm (float): if provided, used to clip gradients

        retain_graph (bool): if True, graph is not freed after backward call

    Returns:
        grad_norms (float): average gradient norms from backpropagation
    """

    # backprop
    optim.zero_grad()
    loss.backward(retain_graph=retain_graph)

    # gradient clipping
    if max_grad_norm is not None:
        torch.nn.utils.clip_grad_norm_(net.parameters(), max_grad_norm)

    # compute grad norms
    grad_norms = 0.0
    for p in net.parameters():
        # only clip gradients for parameters for which requires_grad is True
        if p.grad is not None:
            grad_norms += p.grad.data.norm(2).pow(2).item()

    # step
    optim.step()

    return grad_norms


class dummy_context_mgr:
    """
    A dummy context manager - useful for having conditional scopes (such
    as @maybe_no_grad). Nothing happens in this scope.
    """

    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc_value, traceback):
        return False


def maybe_no_grad(no_grad):
    """
    Args:
        no_grad (bool): if True, the returned context will be torch.no_grad(), otherwise
            it will be a dummy context
    """
    return torch.no_grad() if no_grad else dummy_context_mgr()


def rgetattr(obj, attr, *args):
    "recursively get attributes"

    def _getattr(obj, attr):
        return getattr(obj, attr, *args)

    return functools.reduce(_getattr, [obj] + attr.split("."))


def rsetattr(obj, attr, val):
    "recursively set attributes"
    pre, _, post = attr.rpartition(".")
    return setattr(rgetattr(obj, pre) if pre else obj, post, val)


class ProgressBar(pl.Callback):
    def __init__(
        self, global_progress: bool = True, leave_global_progress: bool = True
    ):
        super().__init__()

        self.global_progress = global_progress
        self.global_desc = "Epoch: {epoch}/{max_epoch}"
        self.leave_global_progress = leave_global_progress
        self.global_pb = None

    def on_fit_start(self, trainer, pl_module):
        desc = self.global_desc.format(
            epoch=trainer.current_epoch + 1, max_epoch=trainer.max_epochs
        )

        self.global_pb = tqdm(
            desc=desc,
            total=trainer.max_epochs,
            initial=trainer.current_epoch,
            leave=self.leave_global_progress,
            disable=not self.global_progress,
        )

    def on_fit_end(self, trainer, pl_module):
        self.global_pb.close()
        self.global_pb = None

    def on_epoch_end(self, trainer, pl_module):

        # Set description
        desc = self.global_desc.format(
            epoch=trainer.current_epoch + 1, max_epoch=trainer.max_epochs
        )
        self.global_pb.set_description(desc)

        # Set logs and metrics
        # logs = pl_module.logs
        # for k, v in logs.items():
        #     if isinstance(v, torch.Tensor):
        #         logs[k] = v.squeeze().item()
        # self.global_pb.set_postfix(logs)

        # Update progress
        self.global_pb.update(1)

def tic(timer: bool = True) -> Union[None, float]:
    """Use to compute time for time-consuming process, call it before .toc()"""

    start_time = None
    if timer:
        torch.cuda.synchronize()
        start_time: float = time.time()

    return start_time


def toc(
    start_time: float, name: str = "", timer: bool = True, log=None
) -> Optional[float]:
    """Use to compute time for time-consuming process, call it after .tic()"""

    if timer:
        torch.cuda.synchronize()
        end_time: float = time.time()
        elapsed_ms: float = (end_time - start_time) * 1000
        print_str: str = f"{name:30s} EP: {elapsed_ms:.2f} ms"

        if log is not None:
            print_log(print_str, log=log, display=False)
        else:
            print(print_str)

        return elapsed_ms

    return None


def print_log(print_str, log, same_line=False, display=True):
	'''
	print a string to a log file

	parameters:
		print_str:          a string to print
		log:                a opened file to save the log
		same_line:          True if we want to print the string without a new next line
		display:            False if we want to disable to print the string onto the terminal
	'''
	if display:
		if same_line: print('{}'.format(print_str), end='')
		else: print('{}'.format(print_str))

	if same_line: log.write('{}'.format(print_str))
	else: log.write('{}\n'.format(print_str))
	log.flush()
        