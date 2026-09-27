(() => {
  const app = document.querySelector("[data-chat-app]");
  if (!app) return;

  const chatLog = document.getElementById("peertrain-chat-log");
  const chatForm = document.querySelector("[data-chat-form]");
  const composer = document.getElementById("id_content");
  const sendButton = document.querySelector("[data-chat-send]");
  const finishButton = document.querySelector("[data-chat-finish]");
  const finishLabel = document.querySelector("[data-chat-finish-label]");
  const actionButtons = document.querySelectorAll("[data-chat-action]");
  const testRepliesToggle = document.querySelector(".peertrain-test-replies-toggle");
  const pendingTemplate = document.querySelector("[data-ai-pending-template]");
  const jumpToLatestButton = document.querySelector("[data-jump-latest]");
  const errorBox = document.querySelector("[data-chat-error]");
  const errorText = document.querySelector("[data-chat-error-text]");
  const retryOpeningButton = document.querySelector("[data-retry-opening]");
  const reloadConversationLink = document.querySelector("[data-reload-conversation]");
  const noticeBox = document.querySelector("[data-chat-notice]");
  const guidanceSidebar = document.querySelector("[data-guidance-sidebar]");
  const guidanceToggles = document.querySelectorAll("[data-guidance-toggle]");
  const guidanceMobileToggle = document.querySelector("[data-guidance-mobile-toggle]");
  const boundaryCoachLayout = document.querySelector("[data-boundary-coach-layout]");
  const boundaryCoach = document.querySelector("[data-boundary-coach]");
  const boundaryCoachPanel = document.querySelector("[data-boundary-coach-panel]");
  const boundaryCoachRail = document.querySelector("[data-boundary-coach-rail]");
  const boundaryCoachToggles = document.querySelectorAll("[data-boundary-coach-toggle]");
  const boundaryCoachMobileTrigger = document.querySelector(".peertrain-boundary-coach-mobile-open");
  const desktopBoundaryCoach = window.matchMedia("(min-width: 1200px)");

  const sessionId = app.dataset.sessionId;
  const userId = app.dataset.userId;
  const maxExchanges = Number(app.dataset.maxExchanges) || 0;
  const guidanceStorageKey = `peertrain:guidance-panel:collapsed:user:${userId}`;
  const boundaryCoachCollapsedKey = `peertrain:boundary-panel:collapsed:user:${userId}`;
  const boundaryCoachLastSeenKey = `peertrain:boundary-panel:last-seen:session:${sessionId}`;

  let currentUserTurns = Number(app.dataset.userTurns) || 0;
  let currentBoundaryScore = Number(app.dataset.boundaryScore) || 0;
  let canSend = app.dataset.canSend === "true";
  let needsOpening = app.dataset.needsOpening === "true";
  let requestPending = false;
  let generatedDraft = null;

  const safeStorageGet = (storage, key) => {
    try {
      return storage.getItem(key);
    } catch (error) {
      return null;
    }
  };

  const safeStorageSet = (storage, key, value) => {
    try {
      storage.setItem(key, value);
    } catch (error) {
      // The interaction still works when browser storage is unavailable.
    }
  };

  const isNearBottom = () => (
    document.documentElement.scrollHeight - (window.scrollY + window.innerHeight) < 180
  );

  const scrollToLatest = (behavior = "smooth") => {
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior });
  };

  const updateJumpToLatest = () => {
    if (!jumpToLatestButton) return;
    jumpToLatestButton.hidden = isNearBottom();
  };

  const autoResizeComposer = () => {
    if (!composer) return;
    composer.style.height = "auto";
    composer.style.height = `${Math.min(composer.scrollHeight, 144)}px`;
  };

  const showError = (message, { retryOpening = false, showReload = true } = {}) => {
    if (!errorBox || !errorText) return;
    errorText.textContent = message;
    if (retryOpeningButton) retryOpeningButton.hidden = !retryOpening;
    if (reloadConversationLink) reloadConversationLink.hidden = !showReload;
    errorBox.hidden = false;
  };

  const clearError = () => {
    if (!errorBox || !errorText) return;
    errorText.textContent = "";
    if (retryOpeningButton) retryOpeningButton.hidden = true;
    if (reloadConversationLink) reloadConversationLink.hidden = false;
    errorBox.hidden = true;
  };

  const showNotice = (message) => {
    if (!noticeBox) return;
    noticeBox.textContent = message || "";
    noticeBox.hidden = !message;
  };

  const setControlsDisabled = (busy) => {
    requestPending = busy;
    const disabled = busy || !canSend || needsOpening;
    if (composer) composer.disabled = disabled;
    if (sendButton) sendButton.disabled = disabled;
    if (testRepliesToggle) testRepliesToggle.disabled = disabled;
    if (finishButton) finishButton.disabled = busy;
    actionButtons.forEach((button) => {
      button.disabled = disabled;
    });
    chatForm?.setAttribute("aria-busy", String(busy));
  };

  const createOptimisticUserMessage = (content, isGeneratedReply) => {
    const article = document.createElement("article");
    article.className = "peertrain-chat-message peertrain-chat-message-user peertrain-chat-message-pending-user";
    article.dataset.pendingUser = "true";

    const stack = document.createElement("div");
    stack.className = "peertrain-chat-message-stack";
    const bubble = document.createElement("div");
    bubble.className = "peertrain-chat-message-bubble";
    bubble.textContent = content;
    const time = document.createElement("time");
    time.className = "peertrain-chat-message-time";
    time.textContent = isGeneratedReply ? "Preparing test reply" : "Sending";

    stack.append(bubble, time);
    article.append(stack);
    chatLog?.append(article);
    return article;
  };

  const createPendingAiMessage = (label = "Responding") => {
    if (!chatLog || !pendingTemplate) return null;
    const fragment = pendingTemplate.content.cloneNode(true);
    const pendingMessage = fragment.querySelector("[data-pending-ai]");
    const pendingLabel = fragment.querySelector("[data-ai-responding-label]");
    if (pendingLabel) pendingLabel.textContent = label;
    chatLog.append(fragment);
    return pendingMessage;
  };

  const updateOptimisticUserMessage = (pendingElement, content, status = "Sending") => {
    if (!pendingElement) return;
    const bubble = pendingElement.querySelector(".peertrain-chat-message-bubble");
    const time = pendingElement.querySelector(".peertrain-chat-message-time");
    if (bubble) bubble.textContent = content;
    if (time) time.textContent = status;
  };

  const replacePendingWithHtml = (pendingElement, html) => {
    if (!pendingElement || !html) return;
    pendingElement.insertAdjacentHTML("beforebegin", html);
    pendingElement.remove();
  };

  const updateTurnCount = (userTurns) => {
    currentUserTurns = Number(userTurns) || 0;
    app.dataset.userTurns = String(currentUserTurns);
    document.querySelectorAll("[data-user-turn-count]").forEach((element) => {
      element.textContent = String(currentUserTurns);
    });
    if (finishButton) {
      finishButton.hidden = currentUserTurns < 1;
      finishButton.dataset.confirmEarly = currentUserTurns < 4 ? "true" : "false";
    }
    if (finishLabel) {
      finishLabel.textContent = currentUserTurns < 4 ? "End early" : "Finish session";
    }
  };

  const updateBoundaryScoreLabels = (score) => {
    currentBoundaryScore = Number(score) || 0;
    app.dataset.boundaryScore = currentBoundaryScore ? String(currentBoundaryScore) : "";
    const shortLabel = currentBoundaryScore ? `S${currentBoundaryScore}` : "–";
    document.querySelectorAll("[data-boundary-rail-score], [data-boundary-mobile-score]").forEach((element) => {
      element.textContent = shortLabel;
    });
  };

  const boundaryCoachUnreadIndicators = () => (
    document.querySelectorAll("[data-boundary-coach-unread]")
  );

  const updateBoundaryCoachUnread = (hasUnread) => {
    boundaryCoachUnreadIndicators().forEach((indicator) => {
      indicator.hidden = !hasUnread;
    });
  };

  const markBoundaryCoachSeen = () => {
    safeStorageSet(window.localStorage, boundaryCoachLastSeenKey, String(currentUserTurns));
    updateBoundaryCoachUnread(false);
  };

  const refreshBoundaryCoachUnread = () => {
    const lastSeenTurns = Number(safeStorageGet(window.localStorage, boundaryCoachLastSeenKey)) || 0;
    updateBoundaryCoachUnread(currentBoundaryScore > 0 && currentUserTurns > lastSeenTurns);
  };

  const updateBoundaryCoach = (html, boundaryScore) => {
    const currentLiveContent = document.querySelector("[data-boundary-coach-live]");
    if (currentLiveContent && html) {
      currentLiveContent.insertAdjacentHTML("beforebegin", html);
      currentLiveContent.remove();
    }
    updateBoundaryScoreLabels(boundaryScore);

    const desktopCollapsed = desktopBoundaryCoach.matches && boundaryCoach?.classList.contains("is-collapsed");
    const mobileClosed = !desktopBoundaryCoach.matches && !boundaryCoach?.classList.contains("show");
    if (desktopCollapsed || mobileClosed) {
      refreshBoundaryCoachUnread();
    } else {
      markBoundaryCoachSeen();
    }
  };

  const setDesktopBoundaryCoachCollapsed = (collapsed, { persist = true, moveFocus = false } = {}) => {
    if (!boundaryCoach || !boundaryCoachLayout || !desktopBoundaryCoach.matches) return;

    boundaryCoach.classList.toggle("is-collapsed", collapsed);
    boundaryCoachLayout.classList.toggle("is-coach-collapsed", collapsed);
    boundaryCoachPanel?.toggleAttribute("inert", collapsed);
    boundaryCoachPanel?.setAttribute("aria-hidden", String(collapsed));
    boundaryCoachRail?.setAttribute("aria-hidden", String(!collapsed));
    boundaryCoachToggles.forEach((toggle) => {
      toggle.setAttribute("aria-expanded", String(!collapsed));
    });

    if (persist) {
      safeStorageSet(window.localStorage, boundaryCoachCollapsedKey, collapsed ? "1" : "0");
    }
    if (collapsed) {
      refreshBoundaryCoachUnread();
    } else {
      markBoundaryCoachSeen();
    }

    if (moveFocus) {
      window.requestAnimationFrame(() => {
        const target = collapsed
          ? boundaryCoach.querySelector(".peertrain-boundary-coach-expand")
          : boundaryCoachPanel?.querySelector("[data-boundary-coach-toggle]");
        target?.focus();
      });
    }
  };

  const syncBoundaryCoachForViewport = () => {
    if (!boundaryCoach || !boundaryCoachLayout) return;
    if (desktopBoundaryCoach.matches) {
      const mobileOffcanvas = window.bootstrap?.Offcanvas?.getInstance(boundaryCoach);
      if (boundaryCoach.classList.contains("show")) mobileOffcanvas?.hide();
      boundaryCoachMobileTrigger?.setAttribute("aria-expanded", "false");
      const collapsed = safeStorageGet(window.localStorage, boundaryCoachCollapsedKey) === "1";
      setDesktopBoundaryCoachCollapsed(collapsed, { persist: false });
      return;
    }

    boundaryCoach.classList.remove("is-collapsed");
    boundaryCoachLayout.classList.remove("is-coach-collapsed");
    boundaryCoachPanel?.removeAttribute("inert");
    boundaryCoachPanel?.removeAttribute("aria-hidden");
    boundaryCoachRail?.setAttribute("aria-hidden", "true");
    refreshBoundaryCoachUnread();
  };

  const chatRequestError = (message, code = "request_failed", status = 0) => {
    const error = new Error(message);
    error.code = code;
    error.status = status;
    return error;
  };

  const requestChatJson = async (formData) => {
    let response;
    try {
      // A form control named "action" shadows the form.action DOM property.
      const chatEndpoint = app.dataset.chatUrl || chatForm?.getAttribute("action") || window.location.href;
      response = await window.fetch(chatEndpoint, {
        method: "POST",
        body: formData,
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
      });
    } catch (networkError) {
      throw chatRequestError(
        "The connection was interrupted. Your message was not retried automatically.",
        "network_error",
      );
    }

    const contentType = response.headers.get("content-type") || "";
    const rawBody = await response.text();
    let payload = null;
    if (contentType.includes("application/json")) {
      try {
        payload = JSON.parse(rawBody);
      } catch (parseError) {
        payload = null;
      }
    }

    if (!payload) {
      if (response.redirected || response.url.includes("/login/")) {
        throw chatRequestError(
          "Your login session has expired. Reload the page and sign in again.",
          "authentication_required",
          response.status,
        );
      }
      if (response.status === 403) {
        throw chatRequestError(
          "The security token has expired. Reload the conversation before trying again.",
          "csrf_failed",
          response.status,
        );
      }
      if (response.status === 502 || response.status === 504) {
        throw chatRequestError(
          "The hosted request timed out while waiting for the model. Your message was not retried.",
          "gateway_timeout",
          response.status,
        );
      }
      if (response.status >= 500) {
        throw chatRequestError(
          "The server could not complete this request. Check the PythonAnywhere error log, then try again.",
          "server_error",
          response.status,
        );
      }
      throw chatRequestError(
        "The chat page and server are out of sync. Reload the page after updating and reloading the web app.",
        "protocol_mismatch",
        response.status,
      );
    }

    if (!response.ok || !payload.ok) {
      if (payload.can_send === false) canSend = false;
      throw chatRequestError(
        payload.error || "The request could not be completed.",
        payload.code || "request_failed",
        response.status,
      );
    }
    return payload;
  };

  const createActionData = (action, fields = {}) => {
    const formData = new FormData(chatForm);
    formData.set("action", action);
    Object.entries(fields).forEach(([key, value]) => formData.set(key, value));
    return formData;
  };

  const joinedNotices = (...notices) => notices.filter(Boolean).join(" ");

  const applySuccessfulTurn = (payload, pendingUser, pendingAi, notice = "") => {
    replacePendingWithHtml(pendingUser, payload.user_message_html);
    replacePendingWithHtml(pendingAi, payload.ai_message_html);
    updateTurnCount(payload.user_turns);
    canSend = Boolean(payload.can_send);
    app.dataset.canSend = String(canSend);
    updateBoundaryCoach(payload.boundary_coach_html, payload.boundary_score);
    showNotice(joinedNotices(notice, payload.notice));
    if (!canSend) {
      showNotice("Maximum exchanges reached. Finish the session to review your feedback.");
    }
  };

  const startRoleplay = async () => {
    if (!chatForm || requestPending || !needsOpening) return;
    clearError();
    showNotice("");
    const pendingAi = createPendingAiMessage("Joining the conversation");
    setControlsDisabled(true);
    scrollToLatest("smooth");

    try {
      const payload = await requestChatJson(createActionData("start_roleplay"));
      replacePendingWithHtml(pendingAi, payload.opening_message_html);
      needsOpening = false;
      app.dataset.needsOpening = "false";
      canSend = Boolean(payload.can_send);
      app.dataset.canSend = String(canSend);
      showNotice(payload.notice || "");
      scrollToLatest("smooth");
    } catch (error) {
      pendingAi?.remove();
      console.error("PeerTrain opening request failed", error);
      showError(
        error.message || "The AI help-seeker could not start the conversation.",
        { retryOpening: true },
      );
    } finally {
      setControlsDisabled(false);
      updateJumpToLatest();
      if (!needsOpening && canSend) composer?.focus();
    }
  };

  const sendManualReply = async (content, responseSource = "") => {
    const followLatest = isNearBottom();
    const pendingUser = createOptimisticUserMessage(content, false);
    const pendingAi = createPendingAiMessage();
    if (followLatest) scrollToLatest("smooth");
    setControlsDisabled(true);

    try {
      const fields = { content };
      if (responseSource) fields.generated_response_source = responseSource;
      const payload = await requestChatJson(createActionData("send", fields));
      applySuccessfulTurn(payload, pendingUser, pendingAi);
      if (composer) composer.value = "";
      generatedDraft = null;
      autoResizeComposer();
      if (followLatest) scrollToLatest("smooth");
    } catch (error) {
      pendingUser?.remove();
      pendingAi?.remove();
      console.error("PeerTrain send request failed", error);
      showError(error.message || "The message could not be sent.");
    } finally {
      setControlsDisabled(false);
      updateJumpToLatest();
      if (canSend) composer?.focus();
    }
  };

  const sendQuickReply = async (quality) => {
    const followLatest = isNearBottom();
    const pendingUser = createOptimisticUserMessage(
      quality === "helpful" ? "Preparing a better test reply..." : "Preparing a poor test reply...",
      true,
    );
    let pendingAi = null;
    let preparedContent = "";
    let preparedSource = "";
    let prepareNotice = "";
    if (followLatest) scrollToLatest("smooth");
    setControlsDisabled(true);

    try {
      const prepared = await requestChatJson(
        createActionData("prepare_auto_reply", { quality }),
      );
      preparedContent = prepared.generated_content;
      preparedSource = prepared.response_source;
      prepareNotice = prepared.notice || "";
      updateOptimisticUserMessage(pendingUser, preparedContent);
      pendingAi = createPendingAiMessage();
      if (followLatest) scrollToLatest("smooth");

      const payload = await requestChatJson(
        createActionData("send", {
          content: preparedContent,
          generated_response_source: preparedSource,
        }),
      );
      applySuccessfulTurn(payload, pendingUser, pendingAi, prepareNotice);
      generatedDraft = null;
      if (composer) composer.value = "";
      autoResizeComposer();
      if (followLatest) scrollToLatest("smooth");
    } catch (error) {
      pendingUser?.remove();
      pendingAi?.remove();
      if (preparedContent && composer) {
        composer.value = preparedContent;
        generatedDraft = { content: preparedContent, source: preparedSource };
        autoResizeComposer();
      }
      console.error("PeerTrain quick reply request failed", error);
      showError(
        preparedContent
          ? `${error.message} The generated test reply has been kept in the message box.`
          : error.message,
      );
    } finally {
      setControlsDisabled(false);
      updateJumpToLatest();
      if (canSend) composer?.focus();
    }
  };

  const handleChatSubmit = async (event) => {
    event.preventDefault();
    if (!chatForm || requestPending) return;

    const submitter = event.submitter || sendButton;
    const action = submitter?.value || "send";
    if (action === "finish") {
      if (currentUserTurns < 1) return;
      if (
        currentUserTurns < 4
        && !window.confirm("End this session early? The feedback may have limited evidence.")
      ) {
        return;
      }
      const finishAction = document.createElement("input");
      finishAction.type = "hidden";
      finishAction.name = "action";
      finishAction.value = "finish";
      chatForm.appendChild(finishAction);
      chatForm.submit();
      return;
    }
    if (needsOpening) {
      showError("Wait for the AI help-seeker to join before replying.", { retryOpening: true });
      return;
    }
    if (!canSend) {
      showError("Maximum exchanges reached. Finish the session to review your feedback.");
      return;
    }
    clearError();
    showNotice("");

    if (action === "auto_helpful" || action === "auto_unhelpful") {
      await sendQuickReply(action === "auto_helpful" ? "helpful" : "unhelpful");
      return;
    }

    const typedContent = composer?.value.trim() || "";
    if (!typedContent) {
      showError("Reply could not be empty.", { showReload: false });
      composer?.focus();
      return;
    }
    const generatedSource = (
      generatedDraft && generatedDraft.content === typedContent
        ? generatedDraft.source
        : ""
    );
    await sendManualReply(typedContent, generatedSource);
  };

  if (chatForm) {
    chatForm.addEventListener("submit", handleChatSubmit);
  }

  if (composer) {
    composer.addEventListener("input", () => {
      if (generatedDraft && composer.value.trim() !== generatedDraft.content) {
        generatedDraft = null;
      }
      autoResizeComposer();
    });
    composer.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        if (!requestPending && canSend && !needsOpening) chatForm?.requestSubmit(sendButton);
      }
    });
    autoResizeComposer();
  }

  retryOpeningButton?.addEventListener("click", startRoleplay);

  jumpToLatestButton?.addEventListener("click", () => {
    scrollToLatest("smooth");
    composer?.focus();
  });
  window.addEventListener("scroll", updateJumpToLatest, { passive: true });
  window.addEventListener("resize", updateJumpToLatest, { passive: true });

  if (boundaryCoach) {
    syncBoundaryCoachForViewport();
    boundaryCoachToggles.forEach((toggle) => {
      toggle.addEventListener("click", () => {
        const shouldCollapse = !boundaryCoach.classList.contains("is-collapsed");
        setDesktopBoundaryCoachCollapsed(shouldCollapse, { moveFocus: true });
      });
    });
    boundaryCoach.addEventListener("shown.bs.offcanvas", () => {
      boundaryCoachMobileTrigger?.setAttribute("aria-expanded", "true");
      markBoundaryCoachSeen();
    });
    boundaryCoach.addEventListener("hidden.bs.offcanvas", () => {
      boundaryCoachMobileTrigger?.setAttribute("aria-expanded", "false");
      if (!desktopBoundaryCoach.matches) boundaryCoachMobileTrigger?.focus();
    });
    desktopBoundaryCoach.addEventListener("change", syncBoundaryCoachForViewport);
  }

  if (guidanceSidebar) {
    const setGuidanceCollapsed = (collapsed) => {
      guidanceSidebar.classList.toggle("is-collapsed", collapsed);
      boundaryCoachLayout?.classList.toggle("is-guidance-collapsed", collapsed);
      guidanceToggles.forEach((toggle) => toggle.setAttribute("aria-expanded", String(!collapsed)));
      safeStorageSet(window.localStorage, guidanceStorageKey, collapsed ? "1" : "0");
    };
    setGuidanceCollapsed(safeStorageGet(window.localStorage, guidanceStorageKey) === "1");
    guidanceToggles.forEach((toggle) => {
      toggle.addEventListener("click", () => {
        if (!window.matchMedia("(min-width: 992px)").matches) {
          guidanceSidebar.classList.remove("is-mobile-open");
          guidanceMobileToggle?.setAttribute("aria-expanded", "false");
          guidanceMobileToggle?.focus();
          return;
        }
        setGuidanceCollapsed(!guidanceSidebar.classList.contains("is-collapsed"));
      });
    });
    guidanceMobileToggle?.addEventListener("click", () => {
      const isOpen = guidanceSidebar.classList.toggle("is-mobile-open");
      guidanceMobileToggle.setAttribute("aria-expanded", String(isOpen));
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || !guidanceSidebar.classList.contains("is-mobile-open")) return;
      guidanceSidebar.classList.remove("is-mobile-open");
      guidanceMobileToggle?.setAttribute("aria-expanded", "false");
      guidanceMobileToggle?.focus();
    });
  }

  setControlsDisabled(false);
  window.requestAnimationFrame(() => {
    scrollToLatest("auto");
    updateJumpToLatest();
    if (needsOpening) window.setTimeout(startRoleplay, 80);
  });
})();
