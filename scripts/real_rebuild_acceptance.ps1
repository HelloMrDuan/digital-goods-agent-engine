param(
    [string]$BaseUrl = "http://127.0.0.1:8080",
    [ValidateRange(1, 20)]
    [int]$Limit = 8,
    [ValidateRange(1, 30)]
    [int]$PollSeconds = 3,
    [ValidateRange(30, 1800)]
    [int]$TimeoutSeconds = 900
)

$ErrorActionPreference = "Stop"

function Invoke-JsonApi {
    param(
        [ValidateSet("GET", "POST")]
        [string]$Method,
        [string]$Path
    )

    $uri = "$BaseUrl$Path"
    if ($Method -eq "POST") {
        return Invoke-RestMethod -Method Post -Uri $uri -ContentType "application/json"
    }
    return Invoke-RestMethod -Method Get -Uri $uri
}

function To-Number {
    param($Value)
    if ($null -eq $Value -or $Value -eq "") { return 0.0 }
    return [double]$Value
}

Write-Host "===== DIGITAL GOODS V1.2.1 REAL REBUILD ACCEPTANCE =====" -ForegroundColor Cyan
Write-Host "BaseUrl=$BaseUrl Limit=$Limit"

Write-Host "`n[1/6] Health" -ForegroundColor Yellow
$health = Invoke-JsonApi -Method GET -Path "/health"
$health | ConvertTo-Json -Depth 10

Write-Host "`n[2/6] Agent-Reach / OpenCLI Doctor" -ForegroundColor Yellow
$doctor = Invoke-JsonApi -Method GET -Path "/api/agent-reach/doctor"
$doctor | ConvertTo-Json -Depth 20

$preCost = $null
try {
    $preCost = Invoke-JsonApi -Method GET -Path "/api/ai/cost?limit=1"
} catch {
    Write-Warning "Cannot read pre-run AI cost: $($_.Exception.Message)"
}

Write-Host "`n[3/6] Start real demand refresh + rebuild + multi-agent analysis" -ForegroundColor Yellow
$rebuild = Invoke-JsonApi -Method POST -Path "/api/opportunities/rebuild?analyze=true&refresh=true&limit=$Limit"
$taskId = [int]$rebuild.task_id
Write-Host "task_id=$taskId concurrency=$($rebuild.concurrency)"

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$task = $null
while ($true) {
    Start-Sleep -Seconds $PollSeconds
    $task = Invoke-JsonApi -Method GET -Path "/api/opportunities/rebuild/tasks/$taskId"
    Write-Host ("[{0}] {1,3}%  {2}  {3}" -f $task.status, $task.progress, $task.stage, $task.message)

    if ($task.status -notin @("PENDING", "RUNNING")) {
        break
    }
    if ((Get-Date) -gt $deadline) {
        throw "Timed out waiting for rebuild task $taskId after $TimeoutSeconds seconds"
    }
}

Write-Host "`n[4/6] Rebuild result" -ForegroundColor Yellow
$result = $task.result
$discovery = $result.demand_discovery
$cluster = $result.cluster_run

$rebuildSummary = [pscustomobject]@{
    TaskId              = $taskId
    Status              = $task.status
    RetainedRecent      = $discovery.retained_recent
    DirectDemandSignals = $discovery.direct_demand_signals
    SuccessfulProbes    = $discovery.successful_probes
    ProbeCount           = $discovery.probe_count
    CandidateGroups      = $cluster.candidate_groups
    DemandEvidence       = $cluster.processed_evidence
    ContextEvidence      = $cluster.context_evidence_considered
    AgentTotal           = $task.total
    AgentSucceeded       = $task.succeeded
    AgentFailed          = $task.failed
    Concurrency          = $task.concurrency
}
$rebuildSummary | Format-List

Write-Host "`n[5/6] Opportunity cards + commercial validation + latest agent usage" -ForegroundColor Yellow
$analysisItems = @($result.agent_analysis)
$cardRows = @()
$runRows = @()

foreach ($item in $analysisItems) {
    $opportunityId = [int]$item.opportunity_id
    try {
        $card = Invoke-JsonApi -Method GET -Path "/api/opportunity-cards/by-opportunity/$opportunityId"
        $validation = Invoke-JsonApi -Method GET -Path "/api/opportunities/$opportunityId/validation"
        $runs = @(Invoke-JsonApi -Method GET -Path "/api/opportunities/$opportunityId/agent-runs?limit=6")

        $cardRows += [pscustomobject]@{
            OpportunityId = $opportunityId
            Decision      = $card.decision
            JudgeProposed = $card.card.judge_proposed_decision
            FinalScore    = $card.final_score
            MarketScore   = $card.market_score
            RiskScore     = $card.risk_score
            Validation    = $validation.status
            ValidationScore = $validation.score
            Recent30d     = $validation.metrics.recent_30d_count
            Signals       = $validation.metrics.effective_signal_count
            Authors       = $validation.metrics.unique_authors
            Platforms     = $validation.metrics.platform_count
            Reasons       = (@($validation.reason_codes) -join ",")
            Title         = $card.title
        }

        foreach ($run in $runs) {
            $runRows += [pscustomobject]@{
                OpportunityId   = $opportunityId
                Agent           = $run.agent_name
                Status          = $run.status
                Provider        = $run.provider
                Model           = $run.model
                PromptTokens    = $run.prompt_tokens
                CompletionTokens = $run.completion_tokens
                TotalTokens     = $run.total_tokens
                DurationMs      = $run.duration_ms
            }
        }
    } catch {
        $cardRows += [pscustomobject]@{
            OpportunityId = $opportunityId
            Decision      = "ERROR"
            JudgeProposed = $null
            FinalScore    = $null
            MarketScore   = $null
            RiskScore     = $null
            Validation    = "ERROR"
            ValidationScore = $null
            Recent30d     = $null
            Signals       = $null
            Authors       = $null
            Platforms     = $null
            Reasons       = $_.Exception.Message
            Title         = ""
        }
    }
}

if ($cardRows.Count -gt 0) {
    $cardRows | Format-Table -AutoSize
} else {
    Write-Host "No recent real-demand opportunity entered the Agent stage."
}

if ($runRows.Count -gt 0) {
    Write-Host "`nLatest agent usage:" -ForegroundColor DarkYellow
    $runRows | Format-Table -AutoSize
    $tokenTotal = ($runRows | Measure-Object -Property TotalTokens -Sum).Sum
    $durationTotalMs = ($runRows | Measure-Object -Property DurationMs -Sum).Sum
    Write-Host "agent_tokens_total=$tokenTotal agent_duration_sum_ms=$durationTotalMs"
}

Write-Host "`n[6/6] AI cost delta + machine-readable report" -ForegroundColor Yellow
$postCost = $null
try {
    $postCost = Invoke-JsonApi -Method GET -Path "/api/ai/cost?limit=200"
} catch {
    Write-Warning "Cannot read post-run AI cost: $($_.Exception.Message)"
}

$preCostValue = if ($preCost) { To-Number $preCost.total_cost_cny } else { 0.0 }
$postCostValue = if ($postCost) { To-Number $postCost.total_cost_cny } else { 0.0 }
$costDelta = [math]::Round(($postCostValue - $preCostValue), 6)
Write-Host "ai_cost_before_cny=$preCostValue ai_cost_after_cny=$postCostValue ai_cost_delta_cny=$costDelta"

$report = [ordered]@{
    generated_at = (Get-Date).ToString("o")
    base_url = $BaseUrl
    health = $health
    doctor = $doctor
    task = $task
    rebuild_summary = $rebuildSummary
    opportunity_cards = $cardRows
    agent_runs = $runRows
    ai_cost_before_cny = $preCostValue
    ai_cost_after_cny = $postCostValue
    ai_cost_delta_cny = $costDelta
}

$reportPath = Join-Path $env:TEMP ("digital-goods-real-rebuild-{0}.json" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$report | ConvertTo-Json -Depth 30 | Set-Content -Path $reportPath -Encoding UTF8
Write-Host "REPORT_FILE=$reportPath" -ForegroundColor Green

if ($task.status -eq "FAILED") {
    exit 2
}
