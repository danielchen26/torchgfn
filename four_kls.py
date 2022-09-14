import os
import torch
from simple_parsing import ArgumentParser
from simple_parsing.helpers.serialization import encode
from tqdm import tqdm, trange

import wandb
from gfn.containers.replay_buffer import ReplayBuffer
from gfn.containers.trajectories import Trajectories
from gfn.envs import HyperGrid
from gfn.estimators import LogitPBEstimator, LogitPFEstimator, LogZEstimator
from gfn.losses import TrajectoryBalance
from gfn.parametrizations import TBParametrization

from gfn.samplers import LogitPFActionsSampler, TrajectoriesSampler
from gfn.samplers.actions_samplers import LogitPBActionsSampler
from gfn.validate import validate

parser = ArgumentParser()
parser.add_argument("--ndim", type=int, default=4)
parser.add_argument("--height", type=int, default=8)
parser.add_argument("--R0", type=float, default=0.1)

parser.add_argument("--batch_size", type=int, default=16)
parser.add_argument("--n_iterations", type=int, default=1000)
parser.add_argument("--lr", type=float, default=0.001)
parser.add_argument("--lr_Z", type=float, default=0.1)
parser.add_argument("--schedule", type=float, default=1.0)
parser.add_argument("--replay_buffer_size", type=int, default=0)
parser.add_argument(
    "--baseline", type=str, choices=["global", "local", "None"], default="None"
)
parser.add_argument("--wandb", type=str, default="")
parser.add_argument("--seed", type=int, default=0)
parser.add_argument("--uniform_pb", action="store_true", default=False)
parser.add_argument("--untied", action="store_true", default=False)
parser.add_argument("--off_policy", action="store_true", default=False)
parser.add_argument(
    "--mode",
    type=str,
    choices=["tb", "forward_kl", "reverse_kl", "rws", "reverse_rws"],
    default="tb",
)
parser.add_argument(
    "--validation_samples",
    type=int,
    default=200000,
    help="Number of validation samples to use to evaluate the pmf.",
)
parser.add_argument("--validation_interval", type=int, default=100)
parser.add_argument("--sample_from_reward", action="store_true", default=False)
parser.add_argument("--reweight", action="store_true", default=False)
parser.add_argument("--no_cuda", action="store_true", default=False)

# Slurm specific arguments
parser.add_argument("--task_id", type=int)
parser.add_argument("--total", type=int)
parser.add_argument("--offset", type=int, default=0)
args = parser.parse_args()

# If launched via slurm scheduler, change the args as follows:
if os.environ.get("SLURM_PROCID") is not None:
    slurm_process_id = int(os.environ["SLURM_PROCID"])
    config_id = args.offset + slurm_process_id * args.total + args.task_id
    print(config_id)
assert False

print(encode(args))

torch.manual_seed(args.seed)
device_str = "cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu"
print(device_str)

env = HyperGrid(args.ndim, args.height, R0=args.R0)


logZ_tensor = torch.tensor(0.0)
logit_PF = LogitPFEstimator(env=env, module_name="NeuralNet")
logit_PB = LogitPBEstimator(
    env=env,
    module_name="Uniform" if args.uniform_pb else "NeuralNet",
    torso=logit_PF.module.torso if not args.untied else None,
)
logZ = LogZEstimator(logZ_tensor)
parametrization = TBParametrization(logit_PF, logit_PB, logZ)

if not args.sample_from_reward:
    if not args.off_policy:
        actions_sampler = LogitPFActionsSampler(estimator=logit_PF)
    else:
        actions_sampler = LogitPFActionsSampler(estimator=logit_PF, temperature=0.1)
else:
    actions_sampler = LogitPBActionsSampler(estimator=logit_PB)
    validation_actions_sampler = LogitPFActionsSampler(estimator=logit_PF)
    validation_trajectories_sampler = TrajectoriesSampler(
        env=env, actions_sampler=validation_actions_sampler
    )

trajectories_sampler = TrajectoriesSampler(env=env, actions_sampler=actions_sampler)

loss_fn = TrajectoryBalance(parametrization=parametrization)

params = [
    {
        "params": [
            val for key, val in parametrization.parameters.items() if key != "logZ"
        ],
        "lr": args.lr,
    }
]
optimizer_Z = torch.optim.Adam([parametrization.parameters["logZ"]], lr=args.lr_Z)
scheduler_Z = torch.optim.lr_scheduler.MultiStepLR(
    optimizer_Z, milestones=list(range(2000, 100000, 2000)), gamma=args.schedule
)

optimizer = torch.optim.Adam(params, lr=args.lr)
if args.mode == "tb":
    optimizer.add_param_group(
        {"params": [parametrization.parameters["logZ"]], "lr": args.lr_Z}
    )
scheduler = torch.optim.lr_scheduler.MultiStepLR(
    optimizer, milestones=list(range(2000, 100000, 2000)), gamma=args.schedule
)


use_replay_buffer = args.replay_buffer_size > 0
if args.replay_buffer_size > 0:
    use_replay_buffer = True
    replay_buffer = ReplayBuffer(
        env, capacity=args.replay_buffer_size, objects="trajectories"
    )


use_wandb = len(args.wandb) > 0


if use_wandb:
    wandb.init(project=args.wandb)
    wandb.config.update(encode(args))
    run_name = args.mode
    run_name += f"_bs_{args.batch_size}_"
    run_name += f"_{args.ndim}_{args.height}_{args.R0}_{args.seed}_"
    wandb.run.name = run_name + wandb.run.name.split("-")[-1]  # type: ignore


visited_terminating_states = env.States()

if (args.mode, args.sample_from_reward, args.reweight) in [
    ("tb", True, True),
    ("tb", False, True),
    ("forward_kl", True, True),
    ("reverse_kl", True, False),
    ("reverse_kl", True, False),
]:
    raise ValueError("Invalid combination of parameters.")
for i in trange(args.n_iterations):
    if args.sample_from_reward:
        samples_idx = torch.distributions.Categorical(probs=env.true_dist_pmf).sample(
            (args.batch_size,)
        )
        states = env.all_states[samples_idx]
        trajectories = trajectories_sampler.sample_trajectories(states=states)
        trajectories = Trajectories.revert_backward_trajectories(trajectories)
        validation_trajectories = validation_trajectories_sampler.sample(
            n_objects=args.batch_size
        )
    else:
        trajectories = trajectories_sampler.sample(n_objects=args.batch_size)

    if use_replay_buffer:
        replay_buffer.add(trajectories)  # type: ignore
        training_objects = replay_buffer.sample(n_objects=args.batch_size)  # type: ignore
    else:
        training_objects = trajectories

    logPF_trajectories, logPB_trajectories, scores = loss_fn.get_scores(trajectories)

    optimizer.zero_grad()

    optimizer_Z.zero_grad()

    if args.baseline == "local":
        baseline = scores.mean().detach()
    elif args.baseline == "global":
        baseline = -logZ_tensor.detach()
    else:
        baseline = 0.0

    loss_Z = (scores.detach() + logZ_tensor).pow(2).mean()
    loss_Z.backward()
    optimizer_Z.step()
    scheduler_Z.step()

    if args.reweight:
        if args.sample_from_reward:
            weights = torch.exp(scores) / torch.exp(scores).sum()
        else:
            weights = torch.exp(-scores) / torch.exp(-scores).sum()
        weights = weights.detach()

    if args.mode == "tb":
        loss = (scores + parametrization.logZ.tensor).pow(2)
    elif args.mode == "forward_kl":
        loss = -logPB_trajectories * (scores.detach() - baseline) - logPF_trajectories
        if args.reweight:
            loss = loss * weights
    elif args.mode == "reverse_kl":
        loss = logPF_trajectories * (scores.detach() - baseline) - logPB_trajectories
    elif args.mode == "rws":
        loss_pf = -logPF_trajectories
        if not args.sample_from_reward and args.reweight:
            loss_pf = loss_pf * weights
        loss_pb = -logPB_trajectories
        if args.sample_from_reward and args.reweight:
            loss_pb = loss_pb * weights
        loss = loss_pf + loss_pb
    elif args.mode == "reverse_rws":
        loss_pf = logPF_trajectories * (scores.detach() - baseline)
        if args.sample_from_reward and args.reweight:
            loss_pf = loss_pf * weights
        loss_pb = -logPB_trajectories * (scores.detach() - baseline)
        if not args.sample_from_reward and args.reweight:
            loss_pb = loss_pb * weights
        loss = loss_pf + loss_pb

    loss = loss.mean()
    loss.backward()
    optimizer.step()
    scheduler.step()

    visited_terminating_states.extend(training_objects.last_states if not args.sample_from_reward else validation_trajectories.last_states)  # type: ignore
    to_log = {"states_visited": (i + 1) * args.batch_size, "loss": loss.item()}
    if use_wandb:
        wandb.log(to_log, step=i)
    if i % args.validation_interval == 0:
        validation_info = validate(
            env, parametrization, args.validation_samples, visited_terminating_states
        )
        if use_wandb:
            wandb.log(validation_info, step=i)
        to_log.update(validation_info)
        tqdm.write(f"{i}: {to_log}")