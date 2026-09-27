(() => {
  document.querySelectorAll('[data-bs-toggle="popover"]').forEach((trigger) => {
    if (window.bootstrap?.Popover) new window.bootstrap.Popover(trigger);
  });

  const dashboardPage = document.querySelector("[data-dashboard-page]");
  if (dashboardPage) {
    const links = [...dashboardPage.querySelectorAll("[data-dashboard-toc-link]")];
    const sections = [...new Set(
      links
        .map((link) => document.querySelector(link.getAttribute("href")))
        .filter(Boolean),
    )];
    const mobileDetails = dashboardPage.querySelector("[data-dashboard-toc-details]");
    const mobileCurrent = dashboardPage.querySelector("[data-dashboard-toc-current]");
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

    const setActiveSection = (sectionId) => {
      links.forEach((link) => {
        const isActive = link.getAttribute("href") === `#${sectionId}`;
        link.classList.toggle("active", isActive);
        if (isActive) link.setAttribute("aria-current", "location");
        else link.removeAttribute("aria-current");
      });
      const activeLink = links.find((link) => link.getAttribute("href") === `#${sectionId}`);
      if (mobileCurrent && activeLink) mobileCurrent.textContent = activeLink.textContent.trim();
    };

    links.forEach((link) => {
      link.addEventListener("click", (event) => {
        const target = document.querySelector(link.getAttribute("href"));
        if (!target) return;
        event.preventDefault();
        target.scrollIntoView({
          behavior: reducedMotion.matches ? "auto" : "smooth",
          block: "start",
        });
        window.history.replaceState(null, "", `#${target.id}`);
        setActiveSection(target.id);
        if (mobileDetails?.open) mobileDetails.open = false;
      });
    });

    let scrollFrame = null;
    const updateActiveFromScroll = () => {
      scrollFrame = null;
      const topOffset = 110;
      let activeSection = sections[0];
      sections.forEach((section) => {
        if (section.getBoundingClientRect().top <= topOffset) activeSection = section;
      });
      const isAtPageEnd = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4;
      if (isAtPageEnd) activeSection = sections[sections.length - 1];
      if (activeSection) setActiveSection(activeSection.id);
    };
    const scheduleActiveUpdate = () => {
      if (scrollFrame !== null) return;
      scrollFrame = window.requestAnimationFrame(updateActiveFromScroll);
    };
    window.addEventListener("scroll", scheduleActiveUpdate, { passive: true });
    window.addEventListener("resize", scheduleActiveUpdate);

    const initialId = window.location.hash.slice(1);
    if (sections.some((section) => section.id === initialId)) setActiveSection(initialId);
    else updateActiveFromScroll();
  }

  const trendDataElement = document.getElementById("boundary-skill-trends-data");
  if (trendDataElement && window.Chart) {
    const trends = JSON.parse(trendDataElement.textContent || "[]");
    trends.forEach((trend) => {
      const canvas = document.getElementById(`boundary-trend-chart-${trend.key}`);
      if (!canvas || !trend.points?.length) return;

      new window.Chart(canvas, {
        type: "line",
        data: {
          labels: trend.points.map((point) => `Attempt ${point.attempt}`),
          datasets: [{
            label: trend.label,
            data: trend.points.map((point) => point.score),
            borderColor: trend.colour,
            backgroundColor: trend.colour,
            borderWidth: 2,
            pointStyle: trend.point_style,
            pointRadius: 4,
            pointHoverRadius: 6,
            showLine: trend.points.length > 1,
            spanGaps: false,
            tension: 0,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { intersect: false, mode: "nearest" },
          scales: {
            y: {
              min: 1,
              max: 5,
              ticks: { stepSize: 1, font: { size: 10 } },
              grid: { color: "#edf0f4" },
            },
            x: {
              grid: { display: false },
              ticks: { maxRotation: 0, font: { size: 10 } },
            },
          },
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                title: (items) => {
                  const point = trend.points[items[0]?.dataIndex];
                  return point ? `Attempt ${point.attempt} · ${point.date}` : "";
                },
                label: (context) => {
                  const point = trend.points[context.dataIndex];
                  return point ? `${point.scenario}: Score ${point.score}` : "";
                },
              },
            },
          },
        },
      });
    });
  }

  const profileCanvas = document.getElementById("boundary-profile-chart");
  const profileDataElement = document.getElementById("boundary-profile-data");
  if (profileCanvas && profileDataElement && window.Chart) {
    const profile = JSON.parse(profileDataElement.textContent || "[]");
    new window.Chart(profileCanvas, {
      type: "radar",
      data: {
        labels: profile.map((item) => item.label.replace(" Boundary", "")),
        datasets: [{
          data: profile.map((item) => item.score),
          borderColor: "#1769e0",
          backgroundColor: "rgba(66, 133, 244, 0.16)",
          borderWidth: 2,
          pointBackgroundColor: ["#4285f4", "#f29900", "#0f9d8a"],
          pointBorderColor: "#ffffff",
          pointRadius: 4,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          r: {
            min: 0,
            max: 5,
            ticks: { stepSize: 1, display: false },
            angleLines: { color: "#dfe5ed" },
            grid: { color: "#e7ebf0" },
            pointLabels: {
              color: "#4f5d73",
              font: { size: 11, weight: "600" },
            },
          },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (context) => `Average score: ${context.parsed.r ?? "Not observed"}`,
            },
          },
        },
      },
    });
  }
})();
