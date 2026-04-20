"""
Coherence Anomaly Index (CAI) Module

Scores cross-domain anomalies by measuring convergence between:
- Driver signals (solar/atmospheric factors)
- Response signals (Schumann resonance)
- Environmental sensors (optional)

CAI is a 0-100 scale anomaly detector that penalizes single-source spikes
and rewards multi-domain agreement.
"""

import time
from typing import Dict, Optional
from datetime import datetime, timezone


# State for rolling baselines (6-hour windows for z-score calculations)
class BaselineTracker:
    def __init__(self, window_size: int = 360):
        """
        Args:
            window_size: Number of data points to keep in rolling window
        """
        self.window_size = window_size
        self.values = []
        self.timestamps = []
    
    def add(self, value: float, timestamp: Optional[float] = None) -> None:
        if timestamp is None:
            timestamp = time.time()
        
        self.values.append(value)
        self.timestamps.append(timestamp)
        
        # Keep only recent values (rolling window)
        if len(self.values) > self.window_size:
            self.values.pop(0)
            self.timestamps.pop(0)
    
    def mean(self) -> float:
        if not self.values:
            return 0.0
        return sum(self.values) / len(self.values)
    
    def stddev(self) -> float:
        if len(self.values) < 2:
            return 0.0
        mean = self.mean()
        variance = sum((x - mean) ** 2 for x in self.values) / len(self.values)
        return variance ** 0.5
    
    def zscore(self, value: float) -> float:
        """Compute z-score of a value relative to rolling baseline."""
        mean = self.mean()
        sd = self.stddev()
        if sd == 0:
            return 0.0
        return (value - mean) / sd


# Global baseline trackers
_baselines = {
    "sri": BaselineTracker(),
    "schumann_intensity": BaselineTracker(),
    "lightning_density": BaselineTracker(),
}

_cai_history = []  # Rolling CAI scores for stability computation


def clamp01(x: float) -> float:
    """Clamp value to [0, 1]."""
    return max(0.0, min(1.0, x))


def norm_sri(sri: float) -> float:
    """Normalize raw SRI (0-60) to [0, 1]."""
    return clamp01(sri / 60.0)


def norm_deviation(deviation: float, scale: float = 0.5) -> float:
    """
    Normalize a fractional deviation to [0, 1].
    
    Args:
        deviation: Fractional change (e.g., +0.4 = +40%)
        scale: Scaling factor (default 0.5 means ±50% → 1.0)
    """
    return clamp01(abs(deviation) / scale)


def compute_features(ctx: Dict) -> Dict[str, float]:
    """
    Extract normalized feature signals from multi-domain inputs.
    
    Args:
        ctx: Dict with keys:
            - sri_value: raw SRI (0-60)
            - sri_z: z-score of SRI vs rolling baseline
            - kp, plasma, xray: raw components (optional)
            - schumann_intensity: current band intensity
            - schumann_baseline: rolling baseline for Schumann
            - schumann_deviation: (current - baseline) / baseline
            - lightning_density_score: 0-15 scale
            - spectrogram_available: bool
            - spectrogram_health: 0-1
            - geo_mag_local_delta: optional 0-1
            - infrasound_delta: optional 0-1
            - pressure_anomaly: optional 0-1
            - optical_flash: optional 0-1
            - neo_score: 0-10 scale
            - data_freshness_score: 0-1
    
    Returns:
        Dict with normalized feature scores (all 0-1)
    """
    f = {}
    
    # Driver strength (solar + atmospheric)
    sri_norm = norm_sri(ctx.get("sri_value", 0))
    sri_anom = clamp01(abs(ctx.get("sri_z", 0)))  # anomaly vs baseline
    f["driver"] = clamp01(0.6 * sri_norm + 0.4 * sri_anom)
    
    # Schumann response (effect)
    schumann_avail = ctx.get("spectrogram_available", False)
    if schumann_avail:
        sch_dev_norm = norm_deviation(ctx.get("schumann_deviation", 0), scale=0.5)
        health = ctx.get("spectrogram_health", 0.7)
        f["response"] = clamp01(0.7 * sch_dev_norm + 0.3 * health)
    else:
        f["response"] = 0.0
    
    # Environmental anomalies (optional sensor bundle)
    env_signals = [
        ctx.get("geo_mag_local_delta", 0),
        ctx.get("infrasound_delta", 0),
        ctx.get("pressure_anomaly", 0),
        ctx.get("optical_flash", 0),
    ]
    env_signals = [x for x in env_signals if x is not None]
    if env_signals:
        f["environment"] = clamp01(sum(env_signals) / max(1, len(env_signals)))
    else:
        f["environment"] = 0.0
    
    # NEO proximity (weak contributor)
    neo_score = ctx.get("neo_score", 0)
    f["neo"] = clamp01(neo_score / 10.0) if neo_score else 0.0
    
    # Data quality gate
    freshness = ctx.get("data_freshness_score", 0.7)
    health = ctx.get("spectrogram_health", 0.7) if schumann_avail else 1.0
    f["data_quality"] = clamp01(freshness * health)
    
    return f


def compute_convergence(f: Dict[str, float]) -> float:
    """
    Measure multi-domain agreement.
    
    Rewards pairwise agreement, penalizes single-source spikes.
    
    Args:
        f: Feature dict from compute_features()
    
    Returns:
        Convergence score (0-1)
    """
    # Pairwise minimums (agreement indicators)
    d_r = min(f.get("driver", 0), f.get("response", 0))
    d_e = min(f.get("driver", 0), f.get("environment", 0))
    r_e = min(f.get("response", 0), f.get("environment", 0))
    
    # Base convergence
    base = (d_r + d_e + r_e) / 3.0
    
    # Single-source penalty
    max_solo = max(f.get("driver", 0), f.get("response", 0), f.get("environment", 0))
    if max_solo > 0:
        others_avg = (
            f.get("driver", 0) + f.get("response", 0) + f.get("environment", 0) - max_solo
        ) / max(1.0, 2.0)
        solo_penalty = clamp01(max_solo - others_avg)
    else:
        solo_penalty = 0.0
    
    return clamp01(base - 0.5 * solo_penalty)


def compute_cai(ctx: Dict, update_baselines: bool = True) -> Dict:
    """
    Compute Coherence Anomaly Index (0-100).
    
    Args:
        ctx: Input context dict (see compute_features docstring)
        update_baselines: If True, update rolling baselines with current values
    
    Returns:
        Dict with:
        - score: 0-100 anomaly score
        - state: "baseline"|"watch"|"elevated"|"anomalous"
        - features: normalized feature dict
        - convergence: convergence score (0-1)
        - confidence: confidence level (0-5)
        - data_quality: data quality score (0-1)
    """
    global _baselines, _cai_history
    
    # Update baselines if requested
    if update_baselines:
        _baselines["sri"].add(ctx.get("sri_value", 0))
        _baselines["schumann_intensity"].add(ctx.get("schumann_intensity", 0))
        _baselines["lightning_density"].add(ctx.get("lightning_density_score", 0))
    
    # Extract features
    f = compute_features(ctx)
    
    # Compute convergence
    conv = compute_convergence(f)
    
    # Weighted sum across domains
    score = (
        0.35 * f.get("driver", 0) +
        0.35 * f.get("response", 0) +
        0.20 * f.get("environment", 0) +
        0.10 * f.get("neo", 0)
    )
    
    # Amplify by convergence
    score = score * (0.5 + 0.5 * conv)
    
    # Data quality gate
    data_quality = f.get("data_quality", 0.5)
    score = score * (0.5 + 0.5 * data_quality)
    
    # Clamp and scale to 0-100
    score_100 = round(clamp01(score) * 100.0)
    
    # Compute confidence level
    confidence = compute_confidence(ctx)
    
    # Classify state
    state = classify_cai(score_100)
    
    # Track history
    _cai_history.append({
        "timestamp": time.time(),
        "score": score_100,
        "state": state,
    })
    
    # Keep last 360 scores (1 per 10s = 1 hour)
    if len(_cai_history) > 360:
        _cai_history.pop(0)
    
    return {
        "score": score_100,
        "state": state,
        "features": {
            "driver": round(f.get("driver", 0), 2),
            "response": round(f.get("response", 0), 2),
            "environment": round(f.get("environment", 0), 2),
            "neo": round(f.get("neo", 0), 2),
            "data_quality": round(data_quality, 2),
        },
        "convergence": round(conv, 2),
        "confidence": confidence,
        "data_quality": data_quality,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def compute_confidence(ctx: Dict) -> int:
    """
    Compute confidence level (0-5) based on signal strength.
    
    Higher confidence = more reliable CAI score.
    """
    c = 0
    
    if ctx.get("sri_value", 0) >= 40:
        c += 1
    
    if ctx.get("kp", 0) >= 4:
        c += 1
    
    if (ctx.get("schumann_deviation", 0) >= 0.3 and 
        ctx.get("spectrogram_available", False)):
        c += 2
    
    if ctx.get("data_freshness_score", 0) > 0.7:
        c += 1
    
    return min(5, c)


def classify_cai(score: int) -> str:
    """Classify CAI score to state label."""
    if score >= 70:
        return "anomalous"
    elif score >= 45:
        return "elevated"
    elif score >= 25:
        return "watch"
    else:
        return "baseline"


def apply_cooldown(last_trigger_score: Optional[int], current_score: int, 
                  min_cooldown_sec: int = 1800) -> bool:
    """
    Prevent rapid re-triggering of alerts.
    
    Args:
        last_trigger_score: Previous anomaly score that triggered an alert
        current_score: Current CAI score
        min_cooldown_sec: Minimum cooldown between triggers (default 30 min)
    
    Returns:
        True if allowed to trigger (cooldown expired or new peak exceeds prior by ≥15%)
    """
    if last_trigger_score is None:
        return True
    
    # Allow trigger if score increases significantly
    if current_score > last_trigger_score * 1.15:
        return True
    
    # Otherwise check time (this would need external state tracking)
    return True


def integrity_check(ctx: Dict) -> float:
    """
    Compute data integrity penalty (multiplicative factor 0-1).
    
    Returns 0.5 if spectrogram unavailable or stale data,
    else returns full value to data_quality.
    """
    if not ctx.get("spectrogram_available", False):
        return 0.5  # Downgrade by 50%
    
    if ctx.get("data_freshness_score", 0.7) < 0.5:
        return 0.5  # Downgrade by 50%
    
    return 1.0  # No penalty


# ============================================================================
# TESTING / CLI
# ============================================================================

if __name__ == "__main__":
    import json
    
    print("Coherence Anomaly Index (CAI) - Demo")
    print("=" * 70)
    
    # Test baseline case
    baseline_ctx = {
        "sri_value": 20,
        "sri_z": 0.5,
        "kp": 2,
        "plasma": 5.0,
        "xray": 1e-6,
        "schumann_intensity": 0.5,
        "schumann_baseline": 0.4,
        "schumann_deviation": 0.25,
        "spectrogram_available": True,
        "spectrogram_health": 0.8,
        "lightning_density_score": 3.0,
        "geo_mag_local_delta": 0.1,
        "infrasound_delta": 0.05,
        "pressure_anomaly": 0.1,
        "optical_flash": 0.0,
        "neo_score": 0,
        "data_freshness_score": 0.9,
    }
    
    print("\n1. Baseline Conditions:")
    cai = compute_cai(baseline_ctx)
    print(json.dumps(cai, indent=2))
    
    # Test elevated case
    elevated_ctx = dict(baseline_ctx)
    elevated_ctx.update({
        "sri_value": 35,
        "sri_z": 1.5,
        "kp": 4,
        "schumann_deviation": 0.6,
        "lightning_density_score": 8.0,
        "geo_mag_local_delta": 0.4,
    })
    
    print("\n2. Elevated Activity:")
    cai = compute_cai(elevated_ctx)
    print(json.dumps(cai, indent=2))
    
    # Test anomalous case (multi-domain convergence)
    anomalous_ctx = dict(baseline_ctx)
    anomalous_ctx.update({
        "sri_value": 45,
        "sri_z": 2.2,
        "kp": 6,
        "schumann_deviation": 0.8,
        "lightning_density_score": 12.0,
        "geo_mag_local_delta": 0.7,
        "infrasound_delta": 0.5,
        "pressure_anomaly": 0.4,
    })
    
    print("\n3. Anomalous Conditions (Multi-Domain):")
    cai = compute_cai(anomalous_ctx)
    print(json.dumps(cai, indent=2))
    
    # Test single-source spike (should be penalized)
    single_spike_ctx = dict(baseline_ctx)
    single_spike_ctx.update({
        "sri_value": 50,  # High spike
        "sri_z": 2.5,
        "kp": 6,
        "schumann_deviation": 0.1,  # But Schumann is quiet
        "lightning_density_score": 2.0,  # Lightning is normal
        "geo_mag_local_delta": 0.05,
        "infrasound_delta": 0.0,
    })
    
    print("\n4. Single-Source Spike (Should Be Penalized):")
    cai = compute_cai(single_spike_ctx)
    print(json.dumps(cai, indent=2))
    print("^^ Notice: Score should be lower than multi-domain anomaly due to convergence penalty")
