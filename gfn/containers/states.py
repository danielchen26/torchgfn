from abc import ABC, ABCMeta, abstractmethod
from typing import Any, Callable, ClassVar, Optional, Tuple, Union

import torch
from torchtyping import TensorType

# Typing
ForwardMasksTensor = TensorType["batch_shape", "n_actions", torch.bool]
BackwardMasksTensor = TensorType["batch_shape", "n_actions - 1", torch.bool]
StatesTensor = TensorType["shape", torch.float]
DonesTensor = TensorType["batch_shape", torch.bool]
OneStateTensor = TensorType["state_shape", torch.float]


class StatesMetaClass(ABCMeta):
    make_initial_states: Callable[[Tuple[int]], StatesTensor]
    make_random_states: Callable[[Tuple[int]], StatesTensor]


class States(metaclass=StatesMetaClass):
    """Base class for states, seen as nodes of the DAG.
    Each environment/task should have its own States subclass.
    States are represented as torch tensors of shape=(*batch_shape , *state_shape).
    """

    n_actions: ClassVar[Optional[int]] = None
    s_0: ClassVar[Optional[OneStateTensor]] = None  # Represents the state s_0
    s_f: ClassVar[
        Optional[OneStateTensor]
    ] = None  # Represents the state s_f, for example it can be torch.tensor([-1., -1., ...])
    state_shape: ClassVar[Optional[Tuple[int]]] = None
    state_ndim: ClassVar[Optional[int]] = None
    device = torch.device(
        "cpu"
    )  # if s_0 is on another device, this will change automatically

    def __init_subclass__(cls, **kwargs) -> None:
        if getattr(cls, "n_actions") is None:
            raise ValueError("n_actions must be specified")
        if not isinstance(getattr(cls, "n_actions"), int):
            raise ValueError("n_actions must be an integer")
        if getattr(cls, "s_0") is None:
            raise ValueError("s_0 must be specified")
        if isinstance(getattr(cls, "s_0"), torch.Tensor):
            state_shape = getattr(cls, "s_0").shape
            setattr(cls, "state_shape", state_shape)
            setattr(cls, "state_ndim", len(state_shape))
            setattr(cls, "device", getattr(cls, "s_0").device)
        else:
            raise ValueError("s_0 must be a torch tensor")
        if getattr(cls, "s_f") is None:
            setattr(cls, "s_f", torch.full_like(getattr(cls, "s_0"), -float("inf")))
        elif isinstance(getattr(cls, "s_f"), torch.Tensor):
            if not getattr(cls, "s_0").shape == getattr(cls, "s_f").shape:
                raise TypeError(f"{cls.__name__}' s_0 and s_f must have the same shape")
        else:
            raise ValueError("s_f must be a torch tensor, or unspecified")

        super().__init_subclass__(**kwargs)

    def __init__(
        self,
        states: Union[StatesTensor, None],
        random_init: bool = False,
        batch_shape: Union[Tuple[int], None] = None,
        is_initial: Union[DonesTensor, None] = None,
        is_sink: Union[DonesTensor, None] = None,
    ) -> None:
        r"""Initialize the states.
        If states is not None,  then random_init and batch_shape are ignored.
        If states is None, then the initialized state is either the
        initial state $s_0$ or random states. In this case, batch_shape cannot be None.
        If random_init is True, then the states are initialized randomly.
        """
        if states is None:
            assert (
                batch_shape is not None
            ), "batch_shape cannot be None if states is None"
            assert (
                is_initial is None and is_sink is None
            ), "when states is None, is_initial, is_sink cannot be None"

            self.batch_shape = batch_shape
            if random_init:
                self.states = self.make_random_states(batch_shape)
                self.is_initial = self.is_initial_state()
                self.is_sink = self.is_sink_state()
            else:
                self.states = self.make_initial_states(batch_shape)
                self.is_initial = torch.ones(batch_shape, dtype=torch.bool).to(
                    self.__class__.device
                )
                self.is_sink = torch.zeros(batch_shape, dtype=torch.bool).to(
                    self.__class__.device
                )
        else:
            self.states = states
            shape = tuple(self.states.shape)
            assert shape[-self.__class__.state_ndim :] == self.__class__.state_shape  # type: ignore
            self.batch_shape = shape[: -self.__class__.state_ndim]  # type: ignore
            if is_initial is None:
                self.is_initial = self.is_initial_state()
            else:
                self.is_initial = is_initial
            if is_sink is None:
                self.is_sink = self.is_sink_state()
            else:
                self.is_sink = is_sink

        self.device = self.states.device
        assert self.device == self.__class__.device

        self.forward_masks: ForwardMasksTensor
        self.backward_masks: BackwardMasksTensor
        self.make_masks()

    def __repr__(self):
        return f"""States(states={self.states},\n forward_masks={self.forward_masks},
        \n backward_masks={self.backward_masks})
        """

    @classmethod
    def make_initial_states(cls, batch_shape: Tuple[int]) -> StatesTensor:
        assert cls.s_0 is not None and cls.state_ndim is not None
        return cls.s_0.repeat(*batch_shape, *((1,) * cls.state_ndim))

    @classmethod
    @abstractmethod
    def make_random_states(cls, batch_shape: Tuple[int]) -> StatesTensor:
        pass

    def is_initial_state(self) -> DonesTensor:
        r"""Return a boolean tensor of shape=(*batch_shape,),
        where True means that the state is $s_0$ of the DAG.
        """
        assert self.__class__.state_ndim is not None
        out = self.states == self.make_initial_states(self.batch_shape)
        for _ in range(self.__class__.state_ndim):
            out = out.all(dim=-1)
        return out

    def is_sink_state(self) -> DonesTensor:
        r"""Return a boolean tensor of shape=(*batch_shape,),
        where True means that the state is $s_f$ of the DAG.
        """
        assert self.__class__.state_ndim is not None and self.__class__.s_f is not None
        sink_states = self.__class__.s_f.repeat(
            *self.batch_shape, *((1,) * self.__class__.state_ndim)
        )
        out = self.states == sink_states

        for _ in range(self.__class__.state_ndim):
            out = out.all(dim=-1)
        return out

    def make_masks(self) -> ForwardMasksTensor:
        assert self.__class__.n_actions is not None
        self.forward_masks = torch.ones(
            (*self.batch_shape, self.__class__.n_actions),
            dtype=torch.bool,
            device=self.device,
        )
        self.backward_masks = torch.ones(
            (*self.batch_shape, self.__class__.n_actions - 1),
            dtype=torch.bool,
            device=self.device,
        )
        self.update_masks()

    @abstractmethod
    def update_masks(self) -> None:
        pass


def make_States_class(
    class_name: str,
    n_actions: int,
    s_0: OneStateTensor,
    s_f: Optional[OneStateTensor],
    make_random_states: Callable[[Any, Tuple[int]], StatesTensor],
    update_masks: Callable[[States], None],
) -> StatesMetaClass:
    """
    Creates a States subclass with the given state_shape and forward/backward mask makers.
    """
    return type(
        class_name,
        (States,),
        {
            "n_actions": n_actions,
            "s_0": s_0,
            "s_f": s_f,
            "make_random_states": make_random_states,
            "update_masks": update_masks,
        },
    )
