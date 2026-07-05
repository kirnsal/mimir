window.RESULTS = {
  "model": "sonnet",
  "repeats": 3,
  "seed": 0,
  "arms": [
    {
      "key": "cold",
      "label": "COLD (no memory)",
      "success_rate": 0.0,
      "min": 0.0,
      "max": 0.0
    },
    {
      "key": "naive",
      "label": "NAIVE (ungated)",
      "success_rate": 0.6666666666666666,
      "min": 0.6666666666666666,
      "max": 0.6666666666666666
    },
    {
      "key": "warm",
      "label": "WARM (Mimir, gated)",
      "success_rate": 1.0,
      "min": 1.0,
      "max": 1.0
    }
  ],
  "tasks": [
    {
      "id": "t-flush",
      "cold": 0,
      "naive": 3,
      "warm": 3
    },
    {
      "id": "t-page",
      "cold": 0,
      "naive": 3,
      "warm": 3
    },
    {
      "id": "t-unit",
      "cold": 0,
      "naive": 0,
      "warm": 3
    }
  ],
  "lift": {
    "cold_to_warm": 1.0,
    "naive_to_warm": 0.33333333333333337
  }
};
