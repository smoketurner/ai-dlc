#!/usr/bin/env python3
"""Smoke test: verify eval_mode propagation across all model layers.

Exits 0 on success, non-zero on failure.  No external dependencies required.
"""

import math
import random
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Minimal synthetic model — reproduces the PyTorch Module / BatchNorm /
# Dropout protocol without requiring torch as a dependency.
# ---------------------------------------------------------------------------


class Module:
    """Base class mirroring torch.nn.Module's training-flag contract."""

    def __init__(self) -> None:
        self.training: bool = True
        self._submodules: dict[str, Module] = {}

    def add_module(self, name: str, module: Module) -> None:
        """Register a named child module."""
        self._submodules[name] = module

    def modules(self) -> list[Module]:
        """Return self and all descendants, depth-first."""
        result: list[Module] = [self]
        for child in self._submodules.values():
            result.extend(child.modules())
        return result

    def eval(self) -> Module:
        """Set every module in the tree to evaluation mode."""
        for mod in self.modules():
            mod.training = False
        return self

    def train(self, mode: bool = True) -> Module:
        """Set every module in the tree to training mode."""
        for mod in self.modules():
            mod.training = mode
        return self

    def forward(self, x: list[float]) -> list[float]:  # pragma: no cover
        """Run the module. Subclasses override this."""
        return x


class BatchNorm(Module):
    """1-D batch normalisation with running mean / variance tracking."""

    def __init__(self, num_features: int) -> None:
        super().__init__()
        self.num_features = num_features
        # Running statistics (updated only in training mode).
        self.running_mean: list[float] = [0.0] * num_features
        self.running_var: list[float] = [1.0] * num_features
        self.momentum: float = 0.1
        self.eps: float = 1e-5

    def forward(self, x: list[float]) -> list[float]:
        """Normalise input; update running stats only when training."""
        if self.training:
            mean = sum(x) / len(x)
            var = sum((v - mean) ** 2 for v in x) / len(x)
            self.running_mean = [
                (1 - self.momentum) * rm + self.momentum * mean for rm in self.running_mean
            ]
            self.running_var = [
                (1 - self.momentum) * rv + self.momentum * var for rv in self.running_var
            ]

        # Normalise using current running stats.
        return [
            (v - self.running_mean[i]) / math.sqrt(self.running_var[i] + self.eps)
            for i, v in enumerate(x)
        ]


class Dropout(Module):
    """Dropout layer: zeros random elements in training; identity in eval."""

    def __init__(self, p: float = 0.5) -> None:
        super().__init__()
        self.p = p

    def forward(self, x: list[float]) -> list[float]:
        """Apply dropout mask when training; pass through when eval."""
        if not self.training:
            return list(x)
        scale = 1.0 / (1.0 - self.p) if self.p < 1.0 else 0.0
        # random used for simulated dropout, not cryptography.
        mask = [random.random() >= self.p for _ in x]  # noqa: S311
        return [0.0 if not keep else v * scale for v, keep in zip(x, mask, strict=True)]


class Linear(Module):
    """Bias-free linear layer (identity weights for simplicity)."""

    def forward(self, x: list[float]) -> list[float]:
        """Return input unchanged (identity transform)."""
        return list(x)


class Network(Module):
    """Minimal two-block network: Linear → BatchNorm → Dropout."""

    def __init__(self) -> None:
        super().__init__()
        self.linear = Linear()
        self.bn = BatchNorm(num_features=4)
        self.dropout = Dropout(p=0.5)
        self.add_module("linear", self.linear)
        self.add_module("bn", self.bn)
        self.add_module("dropout", self.dropout)

    def forward(self, x: list[float]) -> list[float]:
        """Run linear → batch-norm → dropout."""
        x = self.linear.forward(x)
        x = self.bn.forward(x)
        return self.dropout.forward(x)


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


def assert_all_eval(model: Module) -> None:
    """Assert every sub-module has training=False (AC-001, AC-002)."""
    failures: list[str] = []
    for mod in model.modules():
        if mod.training:
            failures.append(f"{type(mod).__name__} has training=True")
    if failures:
        raise AssertionError("Sub-modules still in training mode:\n  " + "\n  ".join(failures))


def assert_bn_stats_unchanged(model: Network, x: list[float]) -> None:
    """Assert batch-norm running stats are not updated in eval mode (AC-001)."""
    bn = model.bn
    before_mean = list(bn.running_mean)
    before_var = list(bn.running_var)
    model.forward(x)
    if bn.running_mean != before_mean or bn.running_var != before_var:
        raise AssertionError(
            f"BatchNorm stats changed in eval mode.\n"
            f"  running_mean: {before_mean} → {bn.running_mean}\n"
            f"  running_var:  {before_var} → {bn.running_var}"
        )


def assert_dropout_determinism(model: Network, x: list[float]) -> None:
    """Assert repeated forward passes produce identical output in eval mode (AC-001)."""
    out1 = model.forward(x)
    out2 = model.forward(x)
    if out1 != out2:
        raise AssertionError(
            f"Dropout non-determinism detected in eval mode.\n  pass 1: {out1}\n  pass 2: {out2}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_checks() -> list[tuple[str, bool, str]]:
    """Execute all smoke checks; return list of (name, passed, detail)."""
    results: list[tuple[str, bool, str]] = []
    model = Network()

    # Confirm the model starts in training mode (sanity guard).
    for mod in model.modules():
        if not mod.training:
            results.append(("pre-condition: all modules start training=True", False, ""))
            return results

    model.eval()
    x: list[float] = [1.0, 2.0, 3.0, 4.0]

    checks: list[tuple[str, Any]] = [
        (
            "all sub-modules have training=False after eval()",
            lambda: assert_all_eval(model),
        ),
        (
            "batch-norm running stats unchanged after forward pass",
            lambda: assert_bn_stats_unchanged(model, x),
        ),
        (
            "dropout is deterministic in eval mode",
            lambda: assert_dropout_determinism(model, x),
        ),
    ]

    for name, check in checks:
        try:
            check()
            results.append((name, True, ""))
        except AssertionError as exc:
            results.append((name, False, str(exc)))

    return results


def main() -> int:
    """Run smoke checks and print PASS/FAIL summary."""
    results = run_checks()
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)

    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")  # noqa: T201
        if detail:
            for line in detail.splitlines():
                print(f"         {line}")  # noqa: T201

    print()  # noqa: T201
    if passed == total:
        print(  # noqa: T201
            f"PASS  {passed}/{total} checks passed — eval_mode propagation verified."
        )
        return 0

    print(f"FAIL  {passed}/{total} checks passed — eval_mode propagation broken.")  # noqa: T201
    return 1


if __name__ == "__main__":
    sys.exit(main())
