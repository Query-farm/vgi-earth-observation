"""Validation and vector presentation for worker-computed optical/radar results."""

import math

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from vgi_notebooks import finish

COLORS = {"field-a": "#287E69", "field-b": "#AB6B3D"}


def validate_coverage(value):
    """Reject nonnumeric or nonfinite coverage thresholds before worker invocation."""
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("MIN_COVERAGE must be a finite number from zero to one.")


def validate_window(value):
    """Use one of two explicit acquisition endpoints for the comparison."""
    if value not in ("2025-09-03T00:00:00Z", "2025-08-22T00:00:00Z"):
        raise ValueError("END_DATE must select the August 22 or September 3 acquisition.")


def plot_coverage(observations, MIN_COVERAGE):
    """Show fixed parcel denominators and selected eligibility threshold."""
    labels = [str(d.date()) for d in pd.to_datetime(sorted(observations.observation_date.unique()))]
    x = np.arange(len(labels))
    fig, ax = plt.subplots(figsize=(11.5, 6.4))
    for offset, (parcel, color) in zip([-0.18, 0.18], COLORS.items(), strict=True):
        data = observations[observations.parcel_id.eq(parcel)].sort_values("observation_date")
        bars = ax.bar(x + offset, data.coverage_fraction * 100, width=0.33, color=color, label=parcel)
        ax.bar_label(
            bars, labels=[f"{r.valid_pixels}/{r.total_pixels}" for r in data.itertuples()], padding=4, fontsize=10
        )
    ax.axhline(MIN_COVERAGE * 100, color="#444444", linestyle="--", label=f"Eligibility cutoff {MIN_COVERAGE:.0%}")
    ax.set(
        xticks=x,
        xticklabels=labels,
        ylim=(0, 118),
        ylabel="Usable grid-cell centres / parcel centres (%)",
        xlabel="Synthetic acquisition label (UTC)",
    )
    fig.legend(*ax.get_legend_handles_labels(), loc="upper left", bbox_to_anchor=(0.07, 0.82), ncol=3, fontsize=9)
    finish(
        fig,
        "Parcel coverage determines observation eligibility",
        f"Minimum usable fraction {MIN_COVERAGE:.0%} · labels are valid / total pixel centres",
        "Synthetic bundled optical scenes · UTC June–July 2026 · Earth Observation v0.1.0\n"
        "Coverage is a sampled fraction, not a probability of cloud-free conditions.",
        name="01-optical-coverage",
    )


def plot_months(observations, monthly, MIN_COVERAGE):
    """Retain missing acquisitions alongside monthly composites."""
    labels = sorted(observations.observation_date.unique())
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 6.5), sharex=True, sharey=True)
    for ax, (parcel, color) in zip(axes, COLORS.items(), strict=True):
        obs = observations[observations.parcel_id.eq(parcel)].sort_values("observation_date")
        mon = monthly[monthly.parcel_id.eq(parcel)].sort_values("month_label")
        ax.plot(
            pd.to_datetime(mon.month_label),
            mon.mean_index,
            linestyle="--",
            color=color,
            alpha=0.6,
            label="Monthly pixel composite",
        )
        for row in obs.itertuples():
            when = pd.Timestamp(row.observation_date)
            if pd.isna(row.mean_index):
                ax.axvspan(when - pd.Timedelta(days=1), when + pd.Timedelta(days=1), color="#dddddd", alpha=0.55)
                ax.text(
                    when,
                    0.04,
                    "No usable pixels",
                    rotation=90,
                    ha="center",
                    va="bottom",
                    transform=ax.get_xaxis_transform(),
                    fontsize=9,
                    color="#555555",
                )
            else:
                ax.scatter(
                    when,
                    row.mean_index,
                    s=95,
                    facecolor=color if row.status == "ok" else "white",
                    edgecolor=color,
                    linewidth=2,
                    zorder=3,
                )
        ax.axhline(0, color="#777777", linewidth=0.8)
        ax.set(title=parcel, ylim=(-0.08, 0.74), xlabel="Synthetic date (UTC)")
        ax.set_xticks(pd.to_datetime(labels), ["Jun 1", "Jun 15", "Jul 1"])
    axes[0].set_ylabel("NDVI (dimensionless)")
    handles = [
        Line2D(
            [0],
            [0],
            marker="o",
            color="none",
            markerfacecolor="#555555",
            markeredgecolor="#555555",
            label="Eligible observation",
        ),
        Line2D(
            [0],
            [0],
            marker="o",
            color="none",
            markerfacecolor="white",
            markeredgecolor="#555555",
            label="Measured, below cutoff",
        ),
        Line2D([0], [0], linestyle="--", color="#777777", label="Monthly composite"),
    ]
    fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.07, 0.82), ncol=3, fontsize=9)
    finish(
        fig,
        "A missing acquisition is not a measured zero",
        f"Filled circles meet {MIN_COVERAGE:.0%} coverage · open circles retain a below-cutoff measurement",
        "Synthetic bundled optical scenes · UTC June–July 2026 · Earth Observation v0.1.0\n"
        "Monthly lines connect composites, not reconstructed daily observations.",
        name="02-optical-months",
    )


def plot_radar(history, endpoint):
    """Separate polarization histories and leave missing acquisitions without a value."""
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 6.5), sharey=True)
    dates = sorted(pd.to_datetime(history.observed_at.unique()))
    for ax, (region, frame) in zip(axes, history.groupby("region_id", sort=True), strict=True):
        for channel, color, marker in [("VV", "#287E69", "o"), ("VH", "#AB6B3D", "s")]:
            data = frame.loc[frame.polarization.eq(channel)].sort_values("observed_at")
            ax.plot(
                pd.to_datetime(data.observed_at),
                data.mean_db,
                marker=marker,
                linewidth=2,
                markersize=7,
                color=color,
                label=channel,
            )
        missing = frame.groupby("observed_at").mean_db.count()
        for when in pd.to_datetime(missing.index[missing.eq(0)]):
            ax.axvspan(when - pd.Timedelta(days=1), when + pd.Timedelta(days=1), color="#D8DFDB", alpha=0.7)
            ax.text(when, -15, "No usable pixels", rotation=90, ha="center", va="center", fontsize=9)
        ax.set_title(region, loc="left", fontsize=12, weight="bold", pad=12)
        ax.set_xticks(dates, [d.strftime("%b %d") for d in dates])
        ax.set_ylim(-34, -6)
        ax.set_xlabel("Synthetic acquisition · 2025 UTC")
        ax.grid(axis="y")
    axes[0].set_ylabel("10 log10(mean linear power) · dB")
    fig.legend(*axes[0].get_legend_handles_labels(), loc="upper left", bbox_to_anchor=(0.07, 0.82), ncol=2)
    finish(
        fig,
        "The latest acquisition can have no usable return",
        f"Window ends {endpoint[:10]} · Sentinel-1a / ascending track 77 · VV and VH kept separate",
        "Synthetic packaged calibrated intensity · not actual satellite measurements · Earth Observation v0.1.0\n"
        "Missing values are gaps, not zero power. Connecting lines compare acquisition summaries only.",
        name="03-radar-history",
    )
