r"""Workflow graph components"""

from functools import lru_cache
from typing import Any, Callable, Dict, Iterator, List, Set, Tuple, Union

from .utils import accepts


class Node(object):
    r"""Abstract graph node"""

    def __init__(self, name: str):
        super().__init__()

        self.name = name

        self._children = {}
        self._parents = {}

    def __repr__(self) -> str:
        return self.name

    def __str__(self) -> str:
        return repr(self)

    def add_child(self, node: 'Node', edge: Any = None) -> None:
        self._children[node] = edge
        node._parents[self] = edge

    def add_parent(self, node: 'Node', edge: Any = None) -> None:
        node.add_child(self, edge)

    def rm_child(self, node: 'Node') -> None:
        del self._children[node]
        del node._parents[self]

    def rm_parent(self, node: 'Node') -> None:
        node.rm_child(self)

    @property
    def children(self) -> List['Node']:
        return list(self._children)

    @property
    def parents(self) -> List['Node']:
        return list(self._parents)


def dfs(*nodes, backward: bool = False) -> Iterator[Node]:
    queue = list(nodes)
    visited = set()

    while queue:
        node = queue.pop()

        if node in visited:
            continue
        else:
            yield node

        queue.extend(node.parents if backward else node.children)
        visited.add(node)


def cycles(*nodes, backward: bool = False) -> Iterator[List[Node]]:
    queue = [list(nodes)]
    path = []
    pathset = set()
    visited = set()

    while queue:
        branch = queue[-1]

        if not branch:
            if not path:
                break

            queue.pop()
            pathset.remove(path.pop())
            continue

        node = branch.pop()

        if node in visited:
            if node in pathset:
                yield path + [node]
            continue

        queue.append(node.parents if backward else node.children)
        path.append(node)
        pathset.add(node)
        visited.add(node)


class Job(Node):
    r"""Job node"""

    def __init__(
        self,
        f: Callable,
        name: str = None,
        array: Union[int, Set[int], range] = None,
        env: List[str] = [],
        settings: Dict[str, Any] = {},
        **kwargs,
    ):
        super().__init__(f.__name__ if name is None else name)

        if type(array) is int:
            array = range(array)

        if array is None:
            assert accepts(f), 'job should not expect arguments'
        else:
            assert accepts(f, 0), 'job array should expect one argument'

        self.f = f
        self.array = array

        # Environment
        self.env = env

        # Settings
        self.settings = settings.copy()
        self.settings.update(kwargs)

        # Dependencies
        self._waitfor = 'all'

        # Postconditions
        self.postconditions = []

        # Skip
        self.skip = False

    @property
    def fn(self) -> Callable:
        name, f, post = self.name, self.f, self.postcondition

        def call(*args) -> Any:
            result = f(*args)

            if post is not None:
                assert post(*args), f'job {name} does not satisfy its postconditions'

            return result

        return call

    def __call__(self, *args) -> Any:
        return self.fn(*args)

    def __repr__(self) -> str:
        if self.array is not None:
            array = self.array

            if type(array) is range:
                array = f'[{array.start}:{array.stop}:{array.step}]'
            else:
                array = '[' + ','.join(map(str, array)) + ']'
        else:
            array = ''

        return self.name + array

    @property
    def dependencies(self) -> Dict['Job', str]:
        return self._parents

    def after(self, *deps, status: str = 'success') -> None:
        assert status in ['success', 'failure', 'any']

        for dep in deps:
            self.add_parent(dep, status)

    def detach(self, *deps) -> None:
        for dep in deps:
            self.rm_parent(dep)

    @property
    def waitfor(self) -> str:
        return self._waitfor

    @waitfor.setter
    def waitfor(self, mode: str = 'all') -> None:
        assert mode in ['all', 'any']

        self._waitfor = mode

    def ensure(self, condition: Callable) -> None:
        if self.array is None:
            assert accepts(condition), 'postcondition should not expect arguments'
        else:
            assert accepts(condition, 0), 'postcondition should expect one argument'

        self.postconditions.append(condition)

    @property
    def postcondition(self) -> Callable:
        conditions = self.postconditions

        if not conditions:
            return None

        if self.array is None:
            def post() -> bool:
                return all(c() for c in conditions)
        else:
            def post(i: int) -> bool:
                return all(c(i) for c in conditions)

        return post

    @lru_cache(None)
    def done(self, i: int = None) -> bool:
        post = self.postcondition

        if post is None:
            return False
        elif self.array is None:
            return post()
        elif i is None:
            return all(map(post, self.array))
        else:
            return post(i)


def prune(*jobs) -> List[Job]:
    for job in dfs(*jobs, backward=True):
        if job.done():
            job.skip = True
            job.detach(*job.dependencies)
        elif job.array is not None:
            pending = {
                i for i in job.array
                if not job.done(i)
            }

            if len(pending) < len(job.array):
                job.array = pending

        done = {
            dep for dep, status in job.dependencies.items()
            if dep.done() and status != 'failure'
        }

        if job.waitfor == 'any' and done:
            job.detach(*job.dependencies)
        elif job.waitfor == 'all':
            job.detach(*done)

    return [
        job for job in jobs
        if not job.done()
    ]
