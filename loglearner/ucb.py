"""Stdlib UCB1 bandit, save-format compatible with the fleet's
AsymmetricUCB (~/agent/core/ucb_bandit.py @ b2dddd2, itself from
UT reference dynamic_sampling.py).

Deliberately dependency-free (no numpy) so the wheel stays stdlib-only; the
JSON snapshot uses the exact same keys, so fleet tooling can load ours and
vice versa. One extension: arms are growable (`ensure_arm`) because
loglearner mints a new arm per KEEP rule over time, whereas the fleet's
bandit is fixed-size at construction.
"""

import json
import math
from pathlib import Path


class AsymmetricUCB:
    def __init__(self, num_arms=0, arm_names=None, c=2.0, epsilon=0.05,
                 decay_rate=0.995):
        self.arm_names = list(arm_names or [f"arm_{i}" for i in range(num_arms)])
        self.num_arms = len(self.arm_names)
        self.c = c
        self.epsilon = epsilon
        self.decay_rate = decay_rate
        self.n_pulls = [0.0] * self.num_arms
        self.n_submitted = [0.0] * self.num_arms
        self.total_reward = [0.0] * self.num_arms
        self.total_reward_sq = [0.0] * self.num_arms
        self.total_steps = 0
        self._baseline_sum = 0.0
        self._baseline_count = 0

    def ensure_arm(self, name):
        """Growable-arm extension: idempotent, returns the arm index."""
        if name in self.arm_names:
            return self.arm_names.index(name)
        self.arm_names.append(name)
        for lst in (self.n_pulls, self.n_submitted,
                    self.total_reward, self.total_reward_sq):
            lst.append(0.0)
        self.num_arms += 1
        return self.num_arms - 1

    def update(self, arm_idx, reward):
        self.n_pulls[arm_idx] += 1
        self.n_submitted[arm_idx] += 1
        self.total_reward[arm_idx] += reward
        self.total_reward_sq[arm_idx] += reward * reward
        self.total_steps += 1
        self._baseline_sum += reward
        self._baseline_count += 1

    def decay(self):
        for lst in (self.n_pulls, self.n_submitted,
                    self.total_reward, self.total_reward_sq):
            for i in range(len(lst)):
                lst[i] *= self.decay_rate

    def ucb_scores(self):
        log_n = math.log(max(self.total_steps, 1))
        scores = []
        for i in range(self.num_arms):
            pulls = max(self.n_pulls[i], 1e-9)
            mean = self.total_reward[i] / pulls if self.n_pulls[i] > 0 else 0.0
            scores.append(mean + self.c * math.sqrt(log_n / pulls))
        return scores

    def select(self):
        if not self.num_arms:
            raise ValueError("no arms")
        scores = self.ucb_scores()
        return max(range(self.num_arms), key=lambda i: scores[i])

    @property
    def baseline(self):
        if not self._baseline_count:
            return 0.0
        return self._baseline_sum / self._baseline_count

    def save(self, path):
        """Identical key set to the fleet implementation's save()."""
        state = {
            "num_arms": self.num_arms,
            "arm_names": self.arm_names,
            "c": self.c,
            "epsilon": self.epsilon,
            "decay_rate": self.decay_rate,
            "n_pulls": list(self.n_pulls),
            "n_submitted": list(self.n_submitted),
            "total_reward": list(self.total_reward),
            "total_reward_sq": list(self.total_reward_sq),
            "total_steps": self.total_steps,
            "baseline_sum": self._baseline_sum,
            "baseline_count": self._baseline_count,
        }
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2))

    @classmethod
    def load(cls, path):
        state = json.loads(Path(path).read_text())
        b = cls(arm_names=state["arm_names"], c=state.get("c", 2.0),
                epsilon=state.get("epsilon", 0.05),
                decay_rate=state.get("decay_rate", 0.995))
        b.n_pulls = list(state["n_pulls"])
        b.n_submitted = list(state["n_submitted"])
        b.total_reward = list(state["total_reward"])
        b.total_reward_sq = list(state["total_reward_sq"])
        b.total_steps = state["total_steps"]
        b._baseline_sum = state.get("baseline_sum", 0.0)
        b._baseline_count = state.get("baseline_count", 0)
        return b
