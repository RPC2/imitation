"""Tests for Behavioural Cloning (BC)."""

import dataclasses
import os

import pytest
import torch as th
from torch.optim import lr_scheduler
from torch.utils import data as th_data

from imitation.algorithms import bc
from imitation.data import rollout, types
from imitation.testing import counter
from imitation.util import util

ROLLOUT_PATH = "tests/data/expert_models/cartpole_0/rollouts/final.pkl"


@pytest.fixture
def venv():
    env_name = "CartPole-v1"
    venv = util.make_vec_env(env_name, 2)
    return venv


@pytest.fixture(params=[32, 50])
def batch_size(request):
    return request.param


@pytest.fixture(params=["data_loader", "ducktyped_data_loader", "transitions"])
def expert_data_type(request):
    return request.param


@pytest.fixture(params=[(None, {}), (lr_scheduler.ExponentialLR, {"gamma": 0.98})])
def lr_sched_tuple(request):
    return request.param


class DucktypedDataset:
    """Used to check that any iterator over Dict[str, Tensor] works with BC."""

    def __init__(self, transitions, batch_size):
        self.trans = transitions
        self.batch_size = batch_size

    def __iter__(self):
        for start in range(0, len(self.trans), self.batch_size):
            d = dict(obs=self.trans.obs, acts=self.trans.acts)
            d = {k: th.from_numpy(v) for k, v in d.items()}
            yield d


@pytest.fixture
def trainer(batch_size, venv, expert_data_type, lr_sched_tuple):
    rollouts = types.load(ROLLOUT_PATH)
    trans = rollout.flatten_trajectories(rollouts)
    lr_sched_cls, lr_sched_kwargs = lr_sched_tuple
    if expert_data_type == "data_loader":
        expert_data = th_data.DataLoader(
            trans,
            batch_size=batch_size,
            shuffle=True,
            collate_fn=types.transitions_collate_fn,
        )
    elif expert_data_type == "ducktyped_data_loader":
        expert_data = DucktypedDataset(trans, batch_size)
    elif expert_data_type == "transitions":
        expert_data = trans
    else:  # pragma: no cover
        raise ValueError(expert_data_type)

    return bc.BC(
        venv.observation_space,
        venv.action_space,
        expert_data=expert_data,
        lr_scheduler_cls=lr_sched_cls,
        lr_scheduler_kwargs=lr_sched_kwargs,
    )


def test_weight_decay_init_error(venv):
    with pytest.raises(ValueError, match=".*weight_decay.*"):
        bc.BC(
            venv.observation_space,
            venv.action_space,
            expert_data=None,
            optimizer_kwargs=dict(weight_decay=1e-4),
        )


def test_train_end_cond_error(trainer: bc.BC, venv):
    err_context = pytest.raises(ValueError, match="exactly one.*n_epochs")
    with err_context:
        trainer.train(n_epochs=1, n_batches=10)
    with err_context:
        trainer.train()
    with err_context:
        trainer.train(n_epochs=None, n_batches=None)


def test_bc(trainer: bc.BC, venv):
    sample_until = rollout.min_episodes(15)
    novice_ret_mean = rollout.mean_return(trainer.policy, venv, sample_until)
    batch_end_count = 0
    epoch_end_count = 0

    def on_batch_end():
        nonlocal batch_end_count
        batch_end_count += 1

    def on_epoch_end():
        nonlocal epoch_end_count
        epoch_end_count += 1

    trainer.train(n_epochs=1, on_epoch_end=on_epoch_end, on_batch_end=on_batch_end)
    assert batch_end_count > 0
    old_batch_end_count = batch_end_count
    assert epoch_end_count == 1
    trainer.train(n_batches=10)
    assert batch_end_count == old_batch_end_count + 10
    trained_ret_mean = rollout.mean_return(trainer.policy, venv, sample_until)
    # Typically <80 score is bad, >350 is okay. We want an improvement of at
    # least 50 points, which seems like it's not noise.
    assert trained_ret_mean - novice_ret_mean > 50


class _DataLoaderFailsOnSecondIter:
    """A dummy DataLoader that yields after a number of calls of `__iter__`.

    Used by `test_bc_data_loader_empty_iter_error`.
    """

    def __init__(self, dummy_yield_value: dict, no_yield_after_iter: int = 1):
        """
        Args:
            no_yield_after_iter: `__iter__` will be
        """
        self.iter_count = 0
        self.dummy_yield_value = dummy_yield_value
        self.no_yield_after_iter = no_yield_after_iter

    def __iter__(self):
        if self.iter_count < self.no_yield_after_iter:
            yield self.dummy_yield_value
        self.iter_count += 1


@pytest.mark.parametrize("no_yield_after_iter", [0, 1, 5])
def test_bc_data_loader_empty_iter_error(venv, no_yield_after_iter):
    """Check that we error out if the DataLoader suddenly stops yielding any batches.

    At one point, we entered an updateless infinite loop in this edge case.
    """
    rollouts = types.load(ROLLOUT_PATH)
    trans = rollout.flatten_trajectories(rollouts)
    dummy_yield_value = dataclasses.asdict(trans[:3])

    bad_data_loader = _DataLoaderFailsOnSecondIter(
        dummy_yield_value=dummy_yield_value,
        no_yield_after_iter=no_yield_after_iter,
    )
    trainer = bc.BC(venv.observation_space, venv.action_space)
    trainer.set_expert_data_loader(bad_data_loader)
    with pytest.raises(AssertionError, match=".*no data.*"):
        trainer.train(n_batches=20)


def test_save_reload(trainer, tmpdir):
    pol_path = os.path.join(tmpdir, "policy.pt")
    var_values = list(trainer.policy.parameters())
    trainer.save_policy(pol_path)
    new_policy = bc.reconstruct_policy(pol_path)
    new_values = list(new_policy.parameters())
    assert len(var_values) == len(new_values)
    for old, new in zip(var_values, new_values):
        assert th.allclose(old, new)


def test_augment(venv):
    rollouts = types.load(ROLLOUT_PATH)
    data = rollout.flatten_trajectories(rollouts)
    mock_augment = counter.IdentityCounter()
    trainer = bc.BC(
        venv.observation_space,
        venv.action_space,
        expert_data=data,
        augmentation_fn=mock_augment,
    )
    assert mock_augment.ncalls == 0
    trainer.train(n_epochs=1)
    assert mock_augment.ncalls > 0
