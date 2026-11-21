from scripts.analysis.analyze_probability_amplitude_model import augment, fit_logistic, predict_model


def test_amplitude_features_are_causal_by_market_order() -> None:
    rows = []
    for market_index in range(5):
        for point_index, distance in enumerate((0.0, 2.0 + market_index, 4.0 + market_index)):
            rows.append({
                "marketId": f"m{market_index}", "marketStartUs": market_index * 100,
                "sourceUs": market_index * 100 + point_index, "distanceBps": distance,
            })
    out = augment(rows)
    first = [row for row in out if row["marketId"] == "m0"]
    fourth = [row for row in out if row["marketId"] == "m4"]
    assert first[0]["historyCount"] == 0
    assert fourth[0]["historyCount"] == 4
    assert fourth[0]["historicalMoveBps"] < 10


def test_logistic_model_returns_probabilities() -> None:
    rows = []
    for index in range(20):
        rows.append({
            "probabilityM0": 0.2 + index / 25,
            "normalizedDistanceBps": -1 + index / 10,
            "labelUp": int(index >= 10),
        })
    model = fit_logistic(rows, ("normalizedDistanceBps",))
    predictions = predict_model(rows, model)
    assert len(predictions) == len(rows)
    assert all(0 < value < 1 for value in predictions)
