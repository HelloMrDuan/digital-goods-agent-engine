param(
    [string]$BaseUrl = "http://127.0.0.1:8080",
    [ValidateRange(1, 20)] [int]$Limit = 8,
    [ValidateRange(1, 8)] [int]$RefreshQueriesPerProbe = 3,
    [ValidateRange(1, 30)] [int]$PollSeconds = 3,
    [ValidateRange(30, 1800)] [int]$TimeoutSeconds = 900
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

Add-Type -AssemblyName System.Net.Http
$script:HttpClient = New-Object System.Net.Http.HttpClient

function Invoke-JsonApi {
    param([ValidateSet("GET", "POST")][string]$Method, [string]$Path)
    $uri = "$BaseUrl$Path"
    $httpMethod = if ($Method -eq "POST") { [System.Net.Http.HttpMethod]::Post } else { [System.Net.Http.HttpMethod]::Get }
    $request = New-Object System.Net.Http.HttpRequestMessage($httpMethod, $uri)
    if ($Method -eq "POST") {
        $request.Content = New-Object System.Net.Http.StringContent("", [System.Text.Encoding]::UTF8, "application/json")
    }
    try {
        $response = $script:HttpClient.SendAsync($request).GetAwaiter().GetResult()
        $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        if (-not $response.IsSuccessStatusCode) {
            throw "HTTP $([int]$response.StatusCode) $($response.ReasonPhrase): $text"
        }
        if ([string]::IsNullOrWhiteSpace($text)) { return $null }
        return $text | ConvertFrom-Json
    } finally {
        $request.Dispose()
    }
}

function To-Number($Value) {
    if ($null -eq $Value -or $Value -eq "") { return 0.0 }
    return [double]$Value
}

Write-Host "===== DIGITAL GOODS V1.2.1 REAL REBUILD ACCEPTANCE =====" -ForegroundColor Cyan
Write-Host "BaseUrl=$BaseUrl Limit=$Limit RefreshQueriesPerProbe=$RefreshQueriesPerProbe"

Write-Host "`n[1/6] Service identity + health" -ForegroundColor Yellow
$openapi = Invoke-JsonApi -Method GET -Path "/openapi.json"
$health = Invoke-JsonApi -Method GET -Path "/health"
$health | ConvertTo-Json -Depth 10
if ($openapi.info.version -ne "1.2.1-opportunity-agent") {
    throw "Wrong/old service instance on $BaseUrl. Expected OpenAPI version 1.2.1-opportunity-agent, got '$($openapi.info.version)'. Restart uvicorn from the current feat/v1.2-opportunity-agent-engine checkout."
}
if (-not $health.llm_configured -or -not $health.qiniu_configured) {
    throw "Qiniu LLM is not configured on the running service."
}

Write-Host "`n[2/6] Agent-Reach / OpenCLI Doctor" -ForegroundColor Yellow
$doctor = Invoke-JsonApi -Method GET -Path "/api/agent-reach/doctor"
$doctor | ConvertTo-Json -Depth 20
$opencliConnected = $false
if ($null -ne $doctor.opencli) { $opencliConnected = [bool]$doctor.opencli.connected }
if (-not $opencliConnected) {
    throw "OpenCLI Browser Bridge is NOT_CONNECTED. Real refresh was NOT started. Connect the Chrome/Chromium extension first; if needed run 'opencli daemon restart', then rerun this script."
}

$preCost = $null
try { $preCost = Invoke-JsonApi -Method GET -Path "/api/ai/cost?limit=1" } catch { Write-Warning $_.Exception.Message }

Write-Host "`n[3/6] Start real refresh + rebuild + multi-agent" -ForegroundColor Yellow
$rebuild = Invoke-JsonApi -Method POST -Path "/api/opportunities/rebuild?analyze=true&refresh=true&limit=$Limit&refresh_queries_per_probe=$RefreshQueriesPerProbe"
$taskId = [int]$rebuild.task_id
Write-Host "task_id=$taskId concurrency=$($rebuild.concurrency)"

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
do {
    Start-Sleep -Seconds $PollSeconds
    $task = Invoke-JsonApi -Method GET -Path "/api/opportunities/rebuild/tasks/$taskId"
    Write-Host ("[{0}] {1,3}%  {2}  {3}" -f $task.status, $task.progress, $task.stage, $task.message)
    if ((Get-Date) -gt $deadline) { throw "Timed out waiting for task $taskId" }
} while ($task.status -in @("PENDING", "RUNNING"))

Write-Host "`n[4/6] Rebuild result" -ForegroundColor Yellow
$result = $task.result
$discovery = $result.demand_discovery
$cluster = $result.cluster_run
$summary = [pscustomobject]@{
    TaskId=$taskId; Status=$task.status
    RetainedRecent=$discovery.retained_recent
    DirectDemandSignals=$discovery.direct_demand_signals
    SuccessfulProbes=$discovery.successful_probes
    ProbeCount=$discovery.probe_count
    CandidateGroups=$cluster.candidate_groups
    DemandEvidence=$cluster.processed_evidence
    ContextEvidence=$cluster.context_evidence_considered
    AgentTotal=$task.total; AgentSucceeded=$task.succeeded; AgentFailed=$task.failed
    Concurrency=$task.concurrency
}
$summary | Format-List

Write-Host "`n[5/6] Opportunity cards + validation + agent usage" -ForegroundColor Yellow
$cardRows = @(); $runRows = @()
foreach ($item in @($result.agent_analysis)) {
    $oid = [int]$item.opportunity_id
    try {
        $card = Invoke-JsonApi -Method GET -Path "/api/opportunity-cards/by-opportunity/$oid"
        $validation = Invoke-JsonApi -Method GET -Path "/api/opportunities/$oid/validation"
        $runs = @(Invoke-JsonApi -Method GET -Path "/api/opportunities/$oid/agent-runs?limit=6")
        $cardRows += [pscustomobject]@{
            OpportunityId=$oid; Decision=$card.decision; FinalScore=$card.final_score
            MarketScore=$card.market_score; RiskScore=$card.risk_score
            Validation=$validation.status; ValidationScore=$validation.score
            Recent30d=$validation.metrics.recent_30d_count
            Signals=$validation.metrics.effective_signal_count
            Authors=$validation.metrics.unique_authors
            Platforms=$validation.metrics.platform_count
            Reasons=(@($validation.reason_codes) -join ",")
            Title=$card.title
        }
        foreach ($run in $runs) {
            $runRows += [pscustomobject]@{
                OpportunityId=$oid; Agent=$run.agent_name; Status=$run.status
                Provider=$run.provider; Model=$run.model
                PromptTokens=$run.prompt_tokens; CompletionTokens=$run.completion_tokens
                TotalTokens=$run.total_tokens; DurationMs=$run.duration_ms
            }
        }
    } catch {
        $cardRows += [pscustomobject]@{ OpportunityId=$oid; Decision="ERROR"; Reasons=$_.Exception.Message }
    }
}
if ($cardRows.Count) { $cardRows | Format-Table -AutoSize } else { Write-Host "No recent real-demand opportunity entered Agent stage." }
if ($runRows.Count) {
    Write-Host "`nLatest agent usage:" -ForegroundColor DarkYellow
    $runRows | Format-Table -AutoSize
    Write-Host "agent_tokens_total=$(($runRows | Measure-Object TotalTokens -Sum).Sum) agent_duration_sum_ms=$(($runRows | Measure-Object DurationMs -Sum).Sum)"
}

Write-Host "`n[6/6] AI cost delta + report" -ForegroundColor Yellow
$postCost = $null
try { $postCost = Invoke-JsonApi -Method GET -Path "/api/ai/cost?limit=200" } catch { Write-Warning $_.Exception.Message }
$before = if ($preCost) { To-Number $preCost.total_cost_cny } else { 0.0 }
$after = if ($postCost) { To-Number $postCost.total_cost_cny } else { 0.0 }
$delta = [math]::Round($after - $before, 6)
Write-Host "ai_cost_before_cny=$before ai_cost_after_cny=$after ai_cost_delta_cny=$delta"

$report = [ordered]@{
    generated_at=(Get-Date).ToString("o"); base_url=$BaseUrl
    openapi_version=$openapi.info.version; health=$health; doctor=$doctor; task=$task
    rebuild_summary=$summary; opportunity_cards=$cardRows; agent_runs=$runRows
    ai_cost_before_cny=$before; ai_cost_after_cny=$after; ai_cost_delta_cny=$delta
}
$reportPath = Join-Path $env:TEMP ("digital-goods-real-rebuild-{0}.json" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$report | ConvertTo-Json -Depth 30 | Set-Content -Path $reportPath -Encoding UTF8
Write-Host "REPORT_FILE=$reportPath" -ForegroundColor Green
if ($task.status -eq "FAILED") { exit 2 }
