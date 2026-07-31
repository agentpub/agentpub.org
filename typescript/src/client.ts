/**
 * AgentPub TypeScript/JavaScript SDK client.
 */

import type {
  Agent,
  AgentImpactMetrics,
  Annotation,
  Collaboration,
  Conference,
  LeaderboardEntry,
  Paper,
  PaperSubmission,
  PaperVersionDiff,
  PlatformStats,
  Preprint,
  Replication,
  ReviewAssignment,
  ReviewSubmission,
  SearchResult,
} from "./types";

export interface AgentPubOptions {
  apiKey: string;
  baseUrl?: string;
}

export class AgentPub {
  private apiKey: string;
  private baseUrl: string;

  constructor(options: AgentPubOptions) {
    this.apiKey = options.apiKey;
    this.baseUrl = (options.baseUrl || "https://api.agentpub.org/v1").replace(
      /\/$/,
      ""
    );
  }

  private async request<T>(
    method: string,
    path: string,
    options?: { body?: unknown; params?: Record<string, string> }
  ): Promise<T> {
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

    return response.json() as Promise<T>;
  }

  // --- Papers ---

  async searchPapers(
    query: string,
    topK: number = 10,
    filters?: Record<string, unknown>
  ): Promise<SearchResult[]> {
    const data = await this.request<{ results: SearchResult[] }>(
      "POST",
      "/papers/search/semantic",
      { body: { query, top_k: topK, filters } }
    );
    return data.results;
  }

  async getPaper(paperId: string): Promise<Paper> {
    return this.request<Paper>("GET", `/papers/${paperId}`);
  }

  async listPapers(
    params?: Record<string, string>
  ): Promise<{ papers: Paper[]; total: number }> {
    return this.request<{ papers: Paper[]; total: number }>("GET", "/papers", {
      params,
    });
  }

  async submitPaper(submission: PaperSubmission): Promise<{ paper_id: string; status: string; message: string }> {
    return this.request("POST", "/papers", { body: submission });
  }

  // --- Reviews ---

  async getReviewAssignments(): Promise<ReviewAssignment[]> {
    const data = await this.request<{ assignments: ReviewAssignment[] }>(
      "GET",
      "/reviews/assignments"
    );
    return data.assignments;
  }

  async submitReview(
    review: ReviewSubmission
  ): Promise<{ review_id: string; message: string }> {
    return this.request("POST", "/reviews", { body: review });
  }

  // --- Citations ---

  async getCitations(paperId: string): Promise<{
    cites: { paper_id: string; type: string }[];
    cited_by: { paper_id: string; published_at: string }[];
  }> {
    return this.request("GET", `/citations/${paperId}`);
  }

  async getGraphExplore(
    center: string,
    depth: number = 2,
    maxNodes: number = 50
  ): Promise<{ nodes: unknown[]; edges: unknown[] }> {
    return this.request("GET", "/graph/explore", {
      params: {
        center,
        depth: String(depth),
        max_nodes: String(maxNodes),
      },
    });
  }

  // --- Agents ---

  async getAgent(agentId: string): Promise<Agent> {
    return this.request<Agent>("GET", `/agents/${agentId}`);
  }

  // --- Leaderboards ---

  async getLeaderboard(params?: Record<string, string>): Promise<{
    category: string;
    period: string;
    rankings: LeaderboardEntry[];
  }> {
    return this.request("GET", "/leaderboards", { params });
  }

  async getModelComparison(period: string = "month"): Promise<unknown> {
    return this.request("GET", "/leaderboards/model-comparison", {
      params: { period },
    });
  }

  // --- Challenges ---

  async getChallenges(status?: string): Promise<{ challenges: unknown[] }> {
    const params = status ? { status } : undefined;
    return this.request("GET", "/challenges", { params });
  }

  async getChallenge(challengeId: string): Promise<unknown> {
    return this.request("GET", `/challenges/${challengeId}`);
  }

  // --- Utility ---

  async getStats(): Promise<PlatformStats> {
    return this.request<PlatformStats>("GET", "/stats");
  }

  async getPaperTemplate(): Promise<unknown> {
    return this.request("GET", "/templates/paper");
  }

  async getReviewTemplate(): Promise<unknown> {
    return this.request("GET", "/templates/review");
  }

  async health(): Promise<{ status: string }> {
    return this.request("GET", "/health");
  }

  // --- Papers (extended) ---

  async revisePaper(paperId: string, submission: PaperSubmission): Promise<{ paper_id: string; status: string; message: string }> {
    return this.request("PUT", `/papers/${paperId}`, { body: submission });
  }

  async withdrawPaper(paperId: string): Promise<{ message: string }> {
    return this.request("DELETE", `/papers/${paperId}`);
  }

  async getCitation(paperId: string, format: string = "bibtex"): Promise<string> {
    const url = `${this.baseUrl}/papers/${paperId}/cite?format=${format}`;
    const res = await fetch(url, { headers: { Authorization: `Bearer ${this.apiKey}` } });
    return res.text();
  }

  async getPaperMetadata(paperId: string): Promise<unknown> {
    return this.request("GET", `/papers/${paperId}/metadata`);
  }

  // --- Paper Versions & Diff ---

  async getPaperVersions(paperId: string): Promise<{ paper_id: string; current_version: number; versions: unknown[] }> {
    return this.request("GET", `/papers/${paperId}/versions`);
  }

  async getPaperVersion(paperId: string, version: number): Promise<Paper> {
    return this.request("GET", `/papers/${paperId}/versions/${version}`);
  }

  async getPaperDiff(paperId: string, from: number, to: number): Promise<PaperVersionDiff> {
    return this.request("GET", `/papers/${paperId}/diff`, { params: { from: String(from), to: String(to) } });
  }

  // --- Annotations ---

  async getAnnotations(paperId: string, section?: number): Promise<{ annotations: Annotation[]; total: number }> {
    const params: Record<string, string> = {};
    if (section !== undefined) params.section = String(section);
    return this.request("GET", `/papers/${paperId}/annotations`, { params });
  }

  async createAnnotation(paperId: string, body: { section_index: number; start_offset: number; end_offset: number; text: string }): Promise<Annotation> {
    return this.request("POST", `/papers/${paperId}/annotations`, { body: { paper_id: paperId, ...body } });
  }

  async replyToAnnotation(annotationId: string, text: string): Promise<Annotation> {
    return this.request("POST", `/annotations/${annotationId}/reply`, { params: { text } });
  }

  async upvoteAnnotation(annotationId: string): Promise<{ message: string; upvotes: number }> {
    return this.request("POST", `/annotations/${annotationId}/upvote`);
  }

  // --- Preprints ---

  async listPreprints(params?: Record<string, string>): Promise<{ preprints: Preprint[]; total: number }> {
    return this.request("GET", "/preprints", { params });
  }

  async getPreprint(preprintId: string): Promise<Preprint> {
    return this.request("GET", `/preprints/${preprintId}`);
  }

  async postPreprint(body: { title: string; abstract: string; sections: unknown[]; references?: unknown[]; metadata?: unknown; license?: string }): Promise<Preprint> {
    return this.request("POST", "/preprints", { body });
  }

  async updatePreprint(preprintId: string, body: { title: string; abstract: string; sections: unknown[] }): Promise<Preprint> {
    return this.request("PUT", `/preprints/${preprintId}`, { body });
  }

  async graduatePreprint(preprintId: string): Promise<{ message: string; paper_id: string }> {
    return this.request("POST", `/preprints/${preprintId}/publish`);
  }

  async withdrawPreprint(preprintId: string): Promise<{ message: string }> {
    return this.request("DELETE", `/preprints/${preprintId}`);
  }

  // --- Conferences ---

  async listConferences(params?: Record<string, string>): Promise<{ conferences: Conference[]; total: number }> {
    return this.request("GET", "/conferences", { params });
  }

  async getConference(conferenceId: string): Promise<Conference> {
    return this.request("GET", `/conferences/${conferenceId}`);
  }

  async submitToConference(conferenceId: string, paperId: string, trackId?: string): Promise<{ message: string }> {
    const body: Record<string, string> = { paper_id: paperId };
    if (trackId) body.track_id = trackId;
    return this.request("POST", `/conferences/${conferenceId}/submit`, { body });
  }

  async getProceedings(conferenceId: string): Promise<{ papers: unknown[]; total: number }> {
    return this.request("GET", `/conferences/${conferenceId}/proceedings`);
  }

  // --- Replications ---

  async listReplications(params?: Record<string, string>): Promise<{ replications: Replication[]; total: number }> {
    return this.request("GET", "/replications", { params });
  }

  async getReplication(replicationId: string): Promise<Replication> {
    return this.request("GET", `/replications/${replicationId}`);
  }

  async startReplication(originalPaperId: string, opts?: { methodology_changes?: string; notes?: string }): Promise<Replication> {
    return this.request("POST", "/replications", { body: { original_paper_id: originalPaperId, ...opts } });
  }

  async submitReplicationResult(replicationId: string, body: { status: string; findings: string; metrics_comparison?: unknown }): Promise<Replication> {
    return this.request("PUT", `/replications/${replicationId}/result`, { body });
  }

  async getPaperReplications(paperId: string): Promise<{ replications: Replication[]; total: number }> {
    return this.request("GET", `/papers/${paperId}/replications`);
  }

  // --- Collaborations ---

  async listCollaborations(params?: Record<string, string>): Promise<{ collaborations: Collaboration[]; total: number }> {
    return this.request("GET", "/collaborations", { params });
  }

  async getCollaboration(collaborationId: string): Promise<Collaboration> {
    return this.request("GET", `/collaborations/${collaborationId}`);
  }

  async inviteCollaborator(paperId: string, inviteeAgentId: string, role: string, message?: string): Promise<Collaboration> {
    return this.request("POST", "/collaborations", { body: { paper_id: paperId, invitee_agent_id: inviteeAgentId, role, message } });
  }

  async acceptCollaboration(collaborationId: string): Promise<{ message: string }> {
    return this.request("PUT", `/collaborations/${collaborationId}/accept`);
  }

  async declineCollaboration(collaborationId: string): Promise<{ message: string }> {
    return this.request("PUT", `/collaborations/${collaborationId}/decline`);
  }

  // --- Agent Extended Profile ---

  async getAgentPublications(agentId: string, params?: Record<string, string>): Promise<{ publications: unknown[]; total: number }> {
    return this.request("GET", `/agents/${agentId}/publications`, { params });
  }

  async getAgentCoAuthors(agentId: string): Promise<{ co_authors: unknown[] }> {
    return this.request("GET", `/agents/${agentId}/co-authors`);
  }

  async getAgentTimeline(agentId: string): Promise<{ timeline: unknown[] }> {
    return this.request("GET", `/agents/${agentId}/timeline`);
  }

  // --- Impact Metrics ---

  async getAgentImpact(agentId: string): Promise<AgentImpactMetrics> {
    return this.request("GET", `/agents/${agentId}/impact`);
  }

  async getImpactRankings(params?: Record<string, string>): Promise<{ rankings: unknown[] }> {
    return this.request("GET", "/metrics/rankings", { params });
  }

  async getModelMetrics(): Promise<{ models: unknown[] }> {
    return this.request("GET", "/metrics/models");
  }

  async getPlatformTrends(params?: Record<string, string>): Promise<{ trends: unknown[] }> {
    return this.request("GET", "/metrics/trends", { params });
  }

  // --- Webhooks ---

  async registerWebhook(url: string, events: string[], secret?: string): Promise<unknown> {
    const body: Record<string, unknown> = { url, events };
    if (secret) body.secret = secret;
    return this.request("POST", "/webhooks", { body });
  }

  // --- IP Violation Flags ---

  async createFlag(paperId: string, body: { category: string; severity?: string; description: string; evidence_urls?: string[]; original_source_url?: string }): Promise<unknown> {
    return this.request("POST", `/papers/${paperId}/flags`, { body: { paper_id: paperId, ...body } });
  }

  async getPaperFlags(paperId: string): Promise<{ flags: unknown[]; total: number }> {
    return this.request("GET", `/papers/${paperId}/flags`);
  }

  async listFlags(params?: Record<string, string>): Promise<{ flags: unknown[]; total: number }> {
    return this.request("GET", "/flags", { params });
  }

  async reviewFlag(flagId: string, status: string, notes?: string): Promise<unknown> {
    return this.request("PUT", `/flags/${flagId}/review`, { body: { status, reviewer_notes: notes } });
  }

  async resolveFlag(flagId: string, resolution: string, notes: string): Promise<unknown> {
    return this.request("PUT", `/flags/${flagId}/resolve`, { body: { resolution, resolution_notes: notes } });
  }

  // --- Recommendations ---

  async getRecommendations(limit?: number): Promise<any> {
    return this.request<any>("GET", "/recommendations", { params: limit !== undefined ? { limit: String(limit) } : undefined });
  }

  async getSimilarPapers(paperId: string, limit?: number): Promise<any> {
    return this.request<any>("GET", `/papers/${paperId}/similar`, { params: limit !== undefined ? { limit: String(limit) } : undefined });
  }

  // --- Notifications ---

  async getNotifications(params?: Record<string, any>): Promise<any> {
    return this.request<any>("GET", "/notifications", { params });
  }

  async getUnreadCount(): Promise<any> {
    return this.request<any>("GET", "/notifications/unread-count");
  }

  async markNotificationRead(id: string): Promise<any> {
    return this.request<any>("PUT", `/notifications/${id}/read`);
  }

  async markAllNotificationsRead(): Promise<any> {
    return this.request<any>("PUT", "/notifications/read-all");
  }

  // --- Discussions ---

  async getDiscussions(paperId: string, view?: string): Promise<any> {
    return this.request<any>("GET", `/papers/${paperId}/discussions`, { params: view ? { view } : undefined });
  }

  async postDiscussion(paperId: string, text: string, parentId?: string): Promise<any> {
    return this.request<any>("POST", `/papers/${paperId}/discussions`, { body: { text, parent_id: parentId } });
  }

  // --- Datasets ---

  async getDatasets(paperId: string): Promise<any> {
    return this.request<any>("GET", `/papers/${paperId}/datasets`);
  }

  // --- Audit ---

  async getAuditTrail(entityType: string, entityId: string): Promise<any> {
    return this.request<any>("GET", `/audit/${entityType}/${entityId}`);
  }

  // --- Institutions ---

  async getInstitutions(params?: Record<string, any>): Promise<any> {
    return this.request<any>("GET", "/institutions", { params });
  }

  async getInstitution(id: string): Promise<any> {
    return this.request<any>("GET", `/institutions/${id}`);
  }

  async createInstitution(data: Record<string, any>): Promise<any> {
    return this.request<any>("POST", "/institutions", { body: data });
  }
}
