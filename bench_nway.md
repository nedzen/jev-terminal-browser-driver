# Jev N-way element choice (OpenRouter decisions, no Laya)

Accuracy is argmax of the **consumed** target head mapped to the executed action id.
Snapshots come from `snapshot.js` via `Browser.observe` on `fixtures/nway.html`.

## N ≈ 20 click targets (≥20 cases)

- Cases: 20 (click-target N ≈ 20)
- Accuracy (consumed action id): 20/20 = 100.0%
- p50 latency: 451 ms
- Mean input tokens: 2549.7
- Mean output tokens: 213.6
- Mean cost USD: 0.0001070874

## N ≈ 100+ click targets

- Cases: 8 (click-target N ≈ 120)
- Accuracy (consumed action id): 8/8 = 100.0%
- p50 latency: 514 ms
- Mean input tokens: 10672
- Mean output tokens: 1035
- Mean cost USD: 0.000448224

## Raw N≈20

```json
[
  {
    "goal": "Click the link labeled Target Item.",
    "correct_id": "e8",
    "choice": "e8",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 1019,
    "input_tokens": 2543,
    "output_tokens": 213,
    "cost": 0.000106806,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 0.",
    "correct_id": "e1",
    "choice": "e1",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 360,
    "input_tokens": 2549,
    "output_tokens": 213,
    "cost": 0.000107058,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 1.",
    "correct_id": "e2",
    "choice": "e2",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 362,
    "input_tokens": 2549,
    "output_tokens": 213,
    "cost": 0.000107058,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 2.",
    "correct_id": "e3",
    "choice": "e3",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 451,
    "input_tokens": 2549,
    "output_tokens": 213,
    "cost": 0.000107058,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 3.",
    "correct_id": "e4",
    "choice": "e4",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 524,
    "input_tokens": 2549,
    "output_tokens": 213,
    "cost": 0.000107058,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 4.",
    "correct_id": "e5",
    "choice": "e5",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 525,
    "input_tokens": 2549,
    "output_tokens": 213,
    "cost": 0.000107058,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 5.",
    "correct_id": "e6",
    "choice": "e6",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 534,
    "input_tokens": 2549,
    "output_tokens": 213,
    "cost": 0.000107058,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 6.",
    "correct_id": "e7",
    "choice": "e7",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 412,
    "input_tokens": 2549,
    "output_tokens": 213,
    "cost": 0.000107058,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 8.",
    "correct_id": "e9",
    "choice": "e9",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 352,
    "input_tokens": 2549,
    "output_tokens": 213,
    "cost": 0.000107058,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 9.",
    "correct_id": "e10",
    "choice": "e10",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 531,
    "input_tokens": 2549,
    "output_tokens": 214,
    "cost": 0.000107058,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 10.",
    "correct_id": "e11",
    "choice": "e11",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 360,
    "input_tokens": 2551,
    "output_tokens": 214,
    "cost": 0.000107142,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 11.",
    "correct_id": "e12",
    "choice": "e12",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 428,
    "input_tokens": 2551,
    "output_tokens": 214,
    "cost": 0.000107142,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 12.",
    "correct_id": "e13",
    "choice": "e13",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 377,
    "input_tokens": 2551,
    "output_tokens": 214,
    "cost": 0.000107142,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 13.",
    "correct_id": "e14",
    "choice": "e14",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 365,
    "input_tokens": 2551,
    "output_tokens": 214,
    "cost": 0.000107142,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 14.",
    "correct_id": "e15",
    "choice": "e15",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 437,
    "input_tokens": 2551,
    "output_tokens": 214,
    "cost": 0.000107142,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 15.",
    "correct_id": "e16",
    "choice": "e16",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 494,
    "input_tokens": 2551,
    "output_tokens": 214,
    "cost": 0.000107142,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 16.",
    "correct_id": "e17",
    "choice": "e17",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 425,
    "input_tokens": 2551,
    "output_tokens": 214,
    "cost": 0.000107142,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 17.",
    "correct_id": "e18",
    "choice": "e18",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 524,
    "input_tokens": 2551,
    "output_tokens": 214,
    "cost": 0.000107142,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 18.",
    "correct_id": "e19",
    "choice": "e19",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 529,
    "input_tokens": 2551,
    "output_tokens": 214,
    "cost": 0.000107142,
    "n_choices": 20
  },
  {
    "goal": "Click the link labeled Filler Item 19.",
    "correct_id": "e20",
    "choice": "e20",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 516,
    "input_tokens": 2551,
    "output_tokens": 214,
    "cost": 0.000107142,
    "n_choices": 20
  }
]
```

## Raw N≈100+

```json
[
  {
    "goal": "Click the link labeled Target Item.",
    "correct_id": "e41",
    "choice": "e41",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 533,
    "input_tokens": 10665,
    "output_tokens": 1035,
    "cost": 0.00044793,
    "n_choices": 120
  },
  {
    "goal": "Click the link labeled Filler Item 0.",
    "correct_id": "e1",
    "choice": "e1",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 514,
    "input_tokens": 10671,
    "output_tokens": 1034,
    "cost": 0.000448182,
    "n_choices": 120
  },
  {
    "goal": "Click the link labeled Filler Item 17.",
    "correct_id": "e18",
    "choice": "e18",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 542,
    "input_tokens": 10673,
    "output_tokens": 1035,
    "cost": 0.000448266,
    "n_choices": 120
  },
  {
    "goal": "Click the link labeled Filler Item 34.",
    "correct_id": "e35",
    "choice": "e35",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 506,
    "input_tokens": 10673,
    "output_tokens": 1035,
    "cost": 0.000448266,
    "n_choices": 120
  },
  {
    "goal": "Click the link labeled Filler Item 52.",
    "correct_id": "e53",
    "choice": "e53",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 524,
    "input_tokens": 10673,
    "output_tokens": 1035,
    "cost": 0.000448266,
    "n_choices": 120
  },
  {
    "goal": "Click the link labeled Filler Item 69.",
    "correct_id": "e70",
    "choice": "e70",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 369,
    "input_tokens": 10673,
    "output_tokens": 1035,
    "cost": 0.000448266,
    "n_choices": 120
  },
  {
    "goal": "Click the link labeled Filler Item 86.",
    "correct_id": "e87",
    "choice": "e87",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 376,
    "input_tokens": 10673,
    "output_tokens": 1035,
    "cost": 0.000448266,
    "n_choices": 120
  },
  {
    "goal": "Click the link labeled Filler Item 103.",
    "correct_id": "e104",
    "choice": "e104",
    "hit": true,
    "operation": "CLICK",
    "latency_ms": 418,
    "input_tokens": 10675,
    "output_tokens": 1036,
    "cost": 0.00044835,
    "n_choices": 120
  }
]
```
