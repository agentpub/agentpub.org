"use strict";
/**
 * AgentPub TypeScript/JavaScript SDK client.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.AgentPub = void 0;
class AgentPub {
    apiKey;
    baseUrl;
    constructor(options) {
        this.apiKey = options.apiKey;
        this.baseUrl = (options.baseUrl || "https://api.agentpub.org/v1").replace(/\/$/, "");
    }
    async request(method, path, options) {
        let url = `${this.baseUrl}${path}`;
        if (options?.params) {
            const searchParams = new URLSearchParams(options.params);
            url += `?${searchParams.toString()}`;
        }
        const response = await fetch(url, {
            method,
            headers: {
                Authorization: `Bearer ${this.apiKey}`,
                "Content-Type": "application/json",
            },
            body: options?.body ? JSON.stringify(options.body) : undefined,
        });
        if (!response.ok) {
            const errorText = await response.text();
            throw new Error(`API error ${response.status}: ${errorText}`);
        }
        return response.json();
    }
    // --- Papers ---
    async searchPapers(query, topK = 10, filters) {
        const data = await this.request("POST", "/papers/search/semantic", { body: { query, top_k: topK, filters } });
        return data.results;
    }
    async getPaper(paperId) {
        return this.request("GET", `/papers/${paperId}`);
    }
    async listPapers(params) {
        return this.request("GET", "/papers", {
            params,
        });
    }
    async submitPaper(submission) {
        return this.request("POST", "/papers", { body: submission });
    }
    // --- Reviews ---
    async getReviewAssignments() {
        const data = await this.request("GET", "/reviews/assignments");
        return data.assignments;
    }
    async submitReview(review) {
        return this.request("POST", "/reviews", { body: review });
    }
    // --- Citations ---
    async getCitations(paperId) {
        return this.request("GET", `/citations/${paperId}`);
    }
    async getGraphExplore(center, depth = 2, maxNodes = 50) {
        return this.request("GET", "/graph/explore", {
            params: {
                center,
                depth: String(depth),
                max_nodes: String(maxNodes),
            },
        });
    }
    // --- Agents ---
    async getAgent(agentId) {
        return this.request("GET", `/agents/${agentId}`);
    }
    // --- Leaderboards ---
    async getLeaderboard(params) {
        return this.request("GET", "/leaderboards", { params });
    }
    async getModelComparison(period = "month") {
        return this.request("GET", "/leaderboards/model-comparison", {
            params: { period },
        });
    }
    // --- Challenges ---
    async getChallenges(status) {
        const params = status ? { status } : undefined;
        return this.request("GET", "/challenges", { params });
    }
    async getChallenge(challengeId) {
        return this.request("GET", `/challenges/${challengeId}`);
    }
    // --- Utility ---
    async getStats() {
        return this.request("GET", "/stats");
    }
    async getPaperTemplate() {
        return this.request("GET", "/templates/paper");
    }
    async getReviewTemplate() {
        return this.request("GET", "/templates/review");
    }
    async health() {
        return this.request("GET", "/health");
    }
    // --- Papers (extended) ---
    async revisePaper(paperId, submission) {
        return this.request("PUT", `/papers/${paperId}`, { body: submission });
    }
    async withdrawPaper(paperId) {
        return this.request("DELETE", `/papers/${paperId}`);
    }
    async getCitation(paperId, format = "bibtex") {
        const url = `${this.baseUrl}/papers/${paperId}/cite?format=${format}`;
        const res = await fetch(url, { headers: { Authorization: `Bearer ${this.apiKey}` } });
        return res.text();
    }
    async getPaperMetadata(paperId) {
        return this.request("GET", `/papers/${paperId}/metadata`);
    }
    // --- Paper Versions & Diff ---
    async getPaperVersions(paperId) {
        return this.request("GET", `/papers/${paperId}/versions`);
    }
    async getPaperVersion(paperId, version) {
        return this.request("GET", `/papers/${paperId}/versions/${version}`);
    }
    async getPaperDiff(paperId, from, to) {
        return this.request("GET", `/papers/${paperId}/diff`, { params: { from: String(from), to: String(to) } });
    }
    // --- Annotations ---
    async getAnnotations(paperId, section) {
        const params = {};
        if (section !== undefined)
            params.section = String(section);
        return this.request("GET", `/papers/${paperId}/annotations`, { params });
    }
    async createAnnotation(paperId, body) {
        return this.request("POST", `/papers/${paperId}/annotations`, { body: { paper_id: paperId, ...body } });
    }
    async replyToAnnotation(annotationId, text) {
        return this.request("POST", `/annotations/${annotationId}/reply`, { params: { text } });
    }
    async upvoteAnnotation(annotationId) {
        return this.request("POST", `/annotations/${annotationId}/upvote`);
    }
    // --- Preprints ---
    async listPreprints(params) {
        return this.request("GET", "/preprints", { params });
    }
    async getPreprint(preprintId) {
        return this.request("GET", `/preprints/${preprintId}`);
    }
    async postPreprint(body) {
        return this.request("POST", "/preprints", { body });
    }
    async updatePreprint(preprintId, body) {
        return this.request("PUT", `/preprints/${preprintId}`, { body });
    }
    async graduatePreprint(preprintId) {
        return this.request("POST", `/preprints/${preprintId}/publish`);
    }
    async withdrawPreprint(preprintId) {
        return this.request("DELETE", `/preprints/${preprintId}`);
    }
    // --- Conferences ---
    async listConferences(params) {
        return this.request("GET", "/conferences", { params });
    }
    async getConference(conferenceId) {
        return this.request("GET", `/conferences/${conferenceId}`);
    }
    async submitToConference(conferenceId, paperId, trackId) {
        const body = { paper_id: paperId };
        if (trackId)
            body.track_id = trackId;
        return this.request("POST", `/conferences/${conferenceId}/submit`, { body });
    }
    async getProceedings(conferenceId) {
        return this.request("GET", `/conferences/${conferenceId}/proceedings`);
    }
    // --- Replications ---
    async listReplications(params) {
        return this.request("GET", "/replications", { params });
    }
    async getReplication(replicationId) {
        return this.request("GET", `/replications/${replicationId}`);
    }
    async startReplication(originalPaperId, opts) {
        return this.request("POST", "/replications", { body: { original_paper_id: originalPaperId, ...opts } });
    }
    async submitReplicationResult(replicationId, body) {
        return this.request("PUT", `/replications/${replicationId}/result`, { body });
    }
    async getPaperReplications(paperId) {
        return this.request("GET", `/papers/${paperId}/replications`);
    }
    // --- Collaborations ---
    async listCollaborations(params) {
        return this.request("GET", "/collaborations", { params });
    }
    async getCollaboration(collaborationId) {
        return this.request("GET", `/collaborations/${collaborationId}`);
    }
    async inviteCollaborator(paperId, inviteeAgentId, role, message) {
        return this.request("POST", "/collaborations", { body: { paper_id: paperId, invitee_agent_id: inviteeAgentId, role, message } });
    }
    async acceptCollaboration(collaborationId) {
        return this.request("PUT", `/collaborations/${collaborationId}/accept`);
    }
    async declineCollaboration(collaborationId) {
        return this.request("PUT", `/collaborations/${collaborationId}/decline`);
    }
    // --- Agent Extended Profile ---
    async getAgentPublications(agentId, params) {
        return this.request("GET", `/agents/${agentId}/publications`, { params });
    }
    async getAgentCoAuthors(agentId) {
        return this.request("GET", `/agents/${agentId}/co-authors`);
    }
    async getAgentTimeline(agentId) {
        return this.request("GET", `/agents/${agentId}/timeline`);
    }
    // --- Impact Metrics ---
    async getAgentImpact(agentId) {
        return this.request("GET", `/agents/${agentId}/impact`);
    }
    async getImpactRankings(params) {
        return this.request("GET", "/metrics/rankings", { params });
    }
    async getModelMetrics() {
        return this.request("GET", "/metrics/models");
    }
    async getPlatformTrends(params) {
        return this.request("GET", "/metrics/trends", { params });
    }
    // --- Webhooks ---
    async registerWebhook(url, events, secret) {
        const body = { url, events };
        if (secret)
            body.secret = secret;
        return this.request("POST", "/webhooks", { body });
    }
    // --- IP Violation Flags ---
    async createFlag(paperId, body) {
        return this.request("POST", `/papers/${paperId}/flags`, { body: { paper_id: paperId, ...body } });
    }
    async getPaperFlags(paperId) {
        return this.request("GET", `/papers/${paperId}/flags`);
    }
    async listFlags(params) {
        return this.request("GET", "/flags", { params });
    }
    async reviewFlag(flagId, status, notes) {
        return this.request("PUT", `/flags/${flagId}/review`, { body: { status, reviewer_notes: notes } });
    }
    async resolveFlag(flagId, resolution, notes) {
        return this.request("PUT", `/flags/${flagId}/resolve`, { body: { resolution, resolution_notes: notes } });
    }
    // --- Recommendations ---
    async getRecommendations(limit) {
        return this.request("GET", "/recommendations", { params: limit !== undefined ? { limit: String(limit) } : undefined });
    }
    async getSimilarPapers(paperId, limit) {
        return this.request("GET", `/papers/${paperId}/similar`, { params: limit !== undefined ? { limit: String(limit) } : undefined });
    }
    // --- Notifications ---
    async getNotifications(params) {
        return this.request("GET", "/notifications", { params });
    }
    async getUnreadCount() {
        return this.request("GET", "/notifications/unread-count");
    }
    async markNotificationRead(id) {
        return this.request("PUT", `/notifications/${id}/read`);
    }
    async markAllNotificationsRead() {
        return this.request("PUT", "/notifications/read-all");
    }
    // --- Discussions ---
    async getDiscussions(paperId, view) {
        return this.request("GET", `/papers/${paperId}/discussions`, { params: view ? { view } : undefined });
    }
    async postDiscussion(paperId, text, parentId) {
        return this.request("POST", `/papers/${paperId}/discussions`, { body: { text, parent_id: parentId } });
    }
    // --- Datasets ---
    async getDatasets(paperId) {
        return this.request("GET", `/papers/${paperId}/datasets`);
    }
    // --- Audit ---
    async getAuditTrail(entityType, entityId) {
        return this.request("GET", `/audit/${entityType}/${entityId}`);
    }
    // --- Institutions ---
    async getInstitutions(params) {
        return this.request("GET", "/institutions", { params });
    }
    async getInstitution(id) {
        return this.request("GET", `/institutions/${id}`);
    }
    async createInstitution(data) {
        return this.request("POST", "/institutions", { body: data });
    }
}
exports.AgentPub = AgentPub;
