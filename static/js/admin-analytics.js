(() => {
  const DEFAULT_PROGRESS_WINDOW = 5;
  const MIN_PROGRESS_WINDOW = 3;

  const scopeSelect = document.querySelector("[data-analytics-scope]");
  if (scopeSelect) {
    scopeSelect.addEventListener("change", () => scopeSelect.form.requestSubmit());
  }

  if (typeof Chart === "undefined") {
    return;
  }

  const parseJsonScript = (id) => {
    const node = document.getElementById(id);
    return node ? JSON.parse(node.textContent) : null;
  };

  const progress = parseJsonScript("admin-progress-data");
  const activity = parseJsonScript("admin-activity-data");
  const dimensionStyles = {
    agency: { color: "#4285f4", pointStyle: "circle" },
    relationship: { color: "#f29900", pointStyle: "rectRounded" },
    safety: { color: "#0f9d8a", pointStyle: "triangle" },
  };

  const progressViewport = document.querySelector("[data-progress-viewport]");
  if (
    progressViewport
    && progress
    && progress.has_visible_data
    && progress.max_attempts > 0
  ) {
    const labelCount = progress.max_attempts;
    const allLabels = Array.from(
      { length: labelCount },
      (_value, index) => `Attempt ${index + 1}`,
    );
    let windowSize = Math.min(DEFAULT_PROGRESS_WINDOW, labelCount);
    let windowStart = Math.max(0, labelCount - windowSize);
    let lastWheelAt = 0;
    const charts = [];

    const valuesByAttempt = (points, valueKey) => {
      const values = Array(labelCount).fill(null);
      points.forEach((point) => {
        if (point.attempt_number <= labelCount) {
          values[point.attempt_number - 1] = point[valueKey];
        }
      });
      return values;
    };

    const visibleDataset = (dataset) => ({
      ...dataset,
      data: dataset.fullData.slice(windowStart, windowStart + windowSize),
      sampleSizes: dataset.fullSampleSizes?.slice(
        windowStart,
        windowStart + windowSize,
      ),
    });

    progress.dimensions.forEach((dimension) => {
      const canvas = document.querySelector(
        `[data-progress-dimension="${dimension.key}"]`,
      );
      if (!canvas) {
        return;
      }

      const style = dimensionStyles[dimension.key];
      const fullDatasets = [];
      const personalData = valuesByAttempt(dimension.personal, "score");
      const cohortData = valuesByAttempt(dimension.cohort, "score");
      const cohortSampleSizes = valuesByAttempt(
        dimension.cohort,
        "sample_size",
      );

      if (progress.mode === "trainee" && dimension.has_personal_data) {
        fullDatasets.push({
          label: `${dimension.label} · selected trainee`,
          fullData: personalData,
          borderColor: style.color,
          backgroundColor: style.color,
          borderWidth: 3,
          pointRadius: 4,
          pointHoverRadius: 6,
          pointStyle: style.pointStyle,
          tension: 0.22,
          spanGaps: false,
          analyticsRole: "personal",
        });
      }
      if (dimension.has_cohort_data) {
        fullDatasets.push({
          label: progress.mode === "trainee"
            ? `${dimension.label} · cohort benchmark`
            : `${dimension.label} · cohort mean`,
          fullData: cohortData,
          fullSampleSizes: cohortSampleSizes,
          borderColor: style.color,
          backgroundColor: style.color,
          borderWidth: progress.mode === "trainee" ? 2 : 3,
          borderDash: progress.mode === "trainee" ? [7, 5] : [],
          pointRadius: progress.mode === "trainee" ? 3 : 4,
          pointHoverRadius: 6,
          pointStyle: style.pointStyle,
          tension: 0.22,
          spanGaps: false,
          analyticsRole: "cohort",
        });
      }

      const chart = new Chart(canvas, {
        type: "line",
        data: {
          labels: allLabels.slice(windowStart, windowStart + windowSize),
          datasets: fullDatasets.map(visibleDataset),
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          animation: { duration: 160 },
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label(context) {
                  const value = context.parsed.y;
                  if (value === null) {
                    return `${context.dataset.label}: not observed`;
                  }
                  if (context.dataset.analyticsRole === "personal") {
                    return `${context.dataset.label}: ${value}/5`;
                  }
                  const sampleSize = context.dataset.sampleSizes?.[
                    context.dataIndex
                  ];
                  return `${context.dataset.label}: ${value}/5 · n=${sampleSize}`;
                },
              },
            },
          },
          scales: {
            y: {
              min: 1,
              max: 5,
              ticks: { stepSize: 1 },
              title: { display: true, text: "Score" },
            },
            x: { grid: { display: false } },
          },
        },
      });
      charts.push({ chart, fullDatasets });
    });

    const previousButton = document.querySelector("[data-progress-previous]");
    const nextButton = document.querySelector("[data-progress-next]");
    const rangeLabel = document.querySelector("[data-progress-range]");
    const zoomStatus = document.querySelector("[data-progress-zoom-status]");
    const scrollControl = document.querySelector("[data-progress-scroll]");
    const scrubberWrap = document.querySelector("[data-progress-scrubber-wrap]");

    const renderWindow = (animationMode = "none") => {
      const maxStart = Math.max(0, labelCount - windowSize);
      windowStart = Math.min(Math.max(0, windowStart), maxStart);
      const windowEnd = Math.min(labelCount, windowStart + windowSize);

      charts.forEach(({ chart, fullDatasets }) => {
        chart.data.labels = allLabels.slice(windowStart, windowEnd);
        chart.data.datasets = fullDatasets.map(visibleDataset);
        chart.update(animationMode);
      });

      if (rangeLabel) {
        rangeLabel.textContent = labelCount === 1
          ? "Attempt 1"
          : `Attempts ${windowStart + 1}–${windowEnd} of ${labelCount}`;
      }
      if (zoomStatus) {
        zoomStatus.textContent = `Showing ${windowSize} of ${labelCount}`;
      }
      if (previousButton) {
        previousButton.disabled = windowStart === 0;
      }
      if (nextButton) {
        nextButton.disabled = windowEnd >= labelCount;
      }
      if (scrollControl) {
        scrollControl.max = String(maxStart);
        scrollControl.value = String(windowStart);
        scrollControl.disabled = maxStart === 0;
      }
      if (scrubberWrap) {
        scrubberWrap.hidden = maxStart === 0;
      }
    };

    const moveWindow = (amount) => {
      windowStart += amount;
      renderWindow();
    };

    const zoomWindow = (amount, anchorRatio = 0.5) => {
      const minimum = Math.min(MIN_PROGRESS_WINDOW, labelCount);
      const nextSize = Math.min(
        labelCount,
        Math.max(minimum, windowSize + amount),
      );
      if (nextSize === windowSize) {
        return;
      }
      const anchorIndex = windowStart + anchorRatio * Math.max(0, windowSize - 1);
      windowSize = nextSize;
      windowStart = Math.round(
        anchorIndex - anchorRatio * Math.max(0, windowSize - 1),
      );
      renderWindow();
    };

    previousButton?.addEventListener("click", () => moveWindow(-1));
    nextButton?.addEventListener("click", () => moveWindow(1));
    scrollControl?.addEventListener("input", (event) => {
      windowStart = Number(event.currentTarget.value);
      renderWindow();
    });

    progressViewport.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault();
        const now = Date.now();
        if (now - lastWheelAt < 100) {
          return;
        }
        lastWheelAt = now;
        progressViewport.classList.add("is-wheel-active");
        window.setTimeout(
          () => progressViewport.classList.remove("is-wheel-active"),
          180,
        );
        if (event.shiftKey) {
          moveWindow(event.deltaY > 0 ? 1 : -1);
          return;
        }
        const bounds = progressViewport.getBoundingClientRect();
        const anchorRatio = bounds.width
          ? Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width))
          : 0.5;
        zoomWindow(event.deltaY > 0 ? 1 : -1, anchorRatio);
      },
      { passive: false },
    );

    progressViewport.addEventListener("keydown", (event) => {
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        moveWindow(-1);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        moveWindow(1);
      } else if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        zoomWindow(-1);
      } else if (event.key === "-" || event.key === "_") {
        event.preventDefault();
        zoomWindow(1);
      } else if (event.key === "Home") {
        event.preventDefault();
        windowStart = 0;
        renderWindow();
      } else if (event.key === "End") {
        event.preventDefault();
        windowStart = labelCount - windowSize;
        renderWindow();
      }
    });

    renderWindow();
  }

  const activityCanvas = document.getElementById("adminActivityChart");
  if (activityCanvas && activity) {
    new Chart(activityCanvas, {
      type: "bar",
      data: {
        labels: activity.map((point) => point.label),
        datasets: [{
          label: "Completed sessions",
          data: activity.map((point) => point.sessions),
          backgroundColor: "#7c3aed",
          borderRadius: 8,
          rangeLabels: activity.map((point) => point.range_label),
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title(items) {
                const item = items[0];
                return item.dataset.rangeLabels[item.dataIndex];
              },
              label(context) {
                const count = context.parsed.y;
                return `${count} completed session${count === 1 ? "" : "s"}`;
              },
            },
          },
        },
        scales: {
          y: { beginAtZero: true, ticks: { precision: 0 } },
          x: { grid: { display: false } },
        },
      },
    });
  }
})();
