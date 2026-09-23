param(
    [string]$BaseUrl = "http://127.0.0.1:8080",
    [ValidateRange(1, 20)] [int]$Limit = 8,
    [ValidateRange(1, 8)] [int]$RefreshQueriesPerProbe = 3,
    [ValidateRange(1, 30)] [int]$PollSeconds = 3,
    [ValidateRange(30, 1800)] [int]$TimeoutSeconds = 900,
    [ValidateRange(0, 2147483647)] [int]$InspectTaskId = 0,
    [switch]$SkipRefresh
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

function To-FlatArray($Value) {
    if ($null -eq $Value) { return @() }
    if ($Value -is [System.Array]) { return @($Value | ForEach-Object { $_ }) }
    return @($Value)
}

function Try-Parse-DateTimeOffset($Value) {
    if ([string]::IsNullOrWhiteSpace([string]$Value)) { return $null }
    $parsed = [DateTimeOffset]::MinValue
    if ([DateTimeOffset]::TryParse([string]$Value, [ref]$parsed)) { return $parsed }
    return $null
}

Write-Host "===== DIGITAL GOODS V1.2.1 REAL REBUILD ACCEPTANCE =====" -ForegroundColor Cyan
Write-Host "BaseUrl=$BaseUrl Limit=$Limit RefreshQueriesPerProbe=$RefreshQueriesPerProbe InspectTaskId=$InspectTaskId SkipRefresh=$SkipRefresh"

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
if (-not $opencliConnected -and $InspectTaskId -eq 0 -and -not $SkipRefresh) {
    throw "OpenCLI Browser Bridge is NOT_CONNECTED. Real refresh was NOT started. Connect the Chrome/Chromium extension first; if needed run 'opencli daemon restart', then rerun this script."
}

$preCost = $null
try { $preCost = Invoke-JsonApi -Method GET -Path "/api/ai/cost?limit=200" } catch { Write-Warning $_.Exception.Message }

Write-Host "`n[3/6] Start/inspect rebuild + multi-agent" -ForegroundColor Yellow
if ($InspectTaskId -gt 0) {
    $taskId = $InspectTaskId
    $task = Invoke-JsonApi -Method GET -Path "/api/opportunities/rebuild/tasks/$taskId"
    Write-Host "inspect existing task_id=$taskId status=$($task.status)"
} else {
    $refreshValue = if ($SkipRefresh) { "false" } else { "true" }
    $rebuild = Invoke-JsonApi -Method POST -Path "/api/opportunities/rebuild?analyze=true&refresh=$refreshValue&limit=$Limit&refresh_queries_per_probe=$RefreshQueriesPerProbe"
    $taskId = [int]$rebuild.task_id
    Write-Host "task_id=$taskId concurrency=$($rebuild.concurrency) refresh=$refreshValue"

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        Start-Sleep -Seconds $PollSeconds
        $task = Invoke-JsonApi -Method GET -Path "/api/opportunities/rebuild/tasks/$taskId"
        Write-Host ("[{0}] {1,3}%  {2}  {3}" -f $task.status, $task.progress, $task.stage, $task.message)
        if ((Get-Date) -gt $deadline) { throw "Timed out waiting for task $taskId" }
    } while ($task.status -in @("PENDING", "RUNNING"))
}

$taskStartedAt = Try-Parse-DateTimeOffset $task.created_at
$taskEndedAt = Try-Parse-DateTimeOffset $task.updated_at

Write-Host "`n[4/6] Rebuild result" -ForegroundColor Yellow
$result = $task.result
$discovery = $result.demand_discovery
$cluster = $result.cluster_run
$summary = [pscustomobject]@{
    TaskId=$taskId; Status=$task.status
    RetainedRecent=if ($discovery) { $discovery.retained_recent } else { $null }
    DirectDemandSignals=if ($discovery) { $discovery.direct_demand_signals } else { $null }
    SuccessfulProbes=if ($discovery) { $discovery.successful_probes } else { $null }
    ProbeCount=if ($discovery) { $discovery.probe_count } else { $null }
    CandidateGroups=if ($cluster) { $cluster.candidate_groups } else { $null }
    DemandEvidence=if ($cluster) { $cluster.processed_evidence } else { $null }
    ContextEvidence=if ($cluster) { $cluster.context_evidence_considered } else { $null }
    AgentTotal=$task.total; AgentSucceeded=$task.succeeded; AgentFailed=$task.failed
    Concurrency=$task.concurrency
}
$summary | Format-List

$analysisItems = @(To-FlatArray $result.agent_analysis)
$analysisFailures = @(
    $analysisItems |
        Where-Object { $_.status -eq "FAILED" -or -not [string]::IsNullOrWhiteSpace([string]$_.error) } |
        ForEach-Object {
            [pscustomobject]@{
                OpportunityId = $_.opportunity_id
                Status = $_.status
                Error = $_.error
            }
        }
)
if ($analysisFailures.Count -gt 0) {
    Write-Host "Rebuild-level analysis failures:" -ForegroundColor Red
    $analysisFailures | Format-Table -Wrap -AutoSize
}

Write-Host "`n[5/6] Opportunity cards + validation + agent usage/errors" -ForegroundColor Yellow
$cardRows = @(); $runRows = @(); $agentFailures = @()
foreach ($item in $analysisItems) {
    $oid = [int]$item.opportunity_id

    $runs = @()
    try {
        $rawRuns = Invoke-JsonApi -Method GET -Path "/api/opportunities/$oid/agent-runs?limit=50"
        $runs = @(To-FlatArray $rawRuns)
        if ($null -ne $taskStartedAt -and $null -ne $taskEndedAt) {
            $runs = @(
                $runs | Where-Object {
                    $created = Try-Parse-DateTimeOffset $_.created_at
                    ($null -eq $created) -or ($created -ge $taskStartedAt -and $created -le $taskEndedAt.AddSeconds(2))
                }
            )
        }
    } catch {
        Write-Warning "Cannot read agent runs for opportunity ${oid}: $($_.Exception.Message)"
    }
    foreach ($run in $runs) {
        $runRows += [pscustomobject]@{
            OpportunityId=$oid; Agent=[string]$run.agent_name; Status=[string]$run.status
            Provider=[string]$run.provider; Model=[string]$run.model
            PromptTokens=To-Number $run.prompt_tokens; CompletionTokens=To-Number $run.completion_tokens
            TotalTokens=To-Number $run.total_tokens; DurationMs=To-Number $run.duration_ms; Error=[string]$run.error
        }
        if ($run.status -eq "FAILED" -or -not [string]::IsNullOrWhiteSpace([string]$run.error)) {
            $agentFailures += [pscustomobject]@{
                OpportunityId=$oid; Agent=[string]$run.agent_name; Error=[string]$run.error; DurationMs=To-Number $run.duration_ms
            }
        }
    }

    $validation = $null
    try { $validation = Invoke-JsonApi -Method GET -Path "/api/opportunities/$oid/validation" } catch { }

    $card = $null
    try { $card = Invoke-JsonApi -Method GET -Path "/api/opportunity-cards/by-opportunity/$oid" } catch { }

    if ($null -ne $card) {
        $cardRows += [pscustomobject]@{
            OpportunityId=$oid; Decision=$card.decision; FinalScore=$card.final_score
            MarketScore=$card.market_score; RiskScore=$card.risk_score
            Validation=if ($validation) { $validation.status } else { $null }
            ValidationScore=if ($validation) { $validation.score } else { $null }
            Recent30d=if ($validation) { $validation.metrics.recent_30d_count } else { $null }
            Signals=if ($validation) { $validation.metrics.effective_signal_count } else { $null }
            Authors=if ($validation) { $validation.metrics.unique_authors } else { $null }
            Platforms=if ($validation) { $validation.metrics.platform_count } else { $null }
            Reasons=if ($validation) { @($validation.reason_codes) -join "," } else { [string]$item.error }
            Title=$card.title
        }
    } else {
        $cardRows += [pscustomobject]@{
            OpportunityId=$oid; Decision="FAILED"; FinalScore=$null; MarketScore=$null; RiskScore=$null
            Validation=if ($validation) { $validation.status } else { $null }
            ValidationScore=if ($validation) { $validation.score } else { $null }
            Recent30d=if ($validation) { $validation.metrics.recent_30d_count } else { $null }
            Signals=if ($validation) { $validation.metrics.effective_signal_count } else { $null }
            Authors=if ($validation) { $validation.metrics.unique_authors } else { $null }
            Platforms=if ($validation) { $validation.metrics.platform_count } else { $null }
            Reasons=[string]$item.error; Title=""
        }
    }
}

if ($cardRows.Count) { $cardRows | Format-Table -Wrap -AutoSize } else { Write-Host "No recent real-demand opportunity entered Agent stage." }
if ($agentFailures.Count) {
    Write-Host "`nFAILED agent runs (root-cause view):" -ForegroundColor Red
    $agentFailures | Sort-Object OpportunityId,Agent | Format-Table -Wrap -AutoSize
}
if ($runRows.Count) {
    Write-Host "`nLatest agent usage (current rebuild task only):" -ForegroundColor DarkYellow
    $runRows | Select-Object OpportunityId,Agent,Status,Provider,Model,PromptTokens,CompletionTokens,TotalTokens,DurationMs | Format-Table -AutoSize
    $tokenTotal = ($runRows | ForEach-Object { To-Number $_.TotalTokens } | Measure-Object -Sum).Sum
    $durationTotalMs = ($runRows | ForEach-Object { To-Number $_.DurationMs } | Measure-Object -Sum).Sum
    Write-Host "agent_tokens_total=$tokenTotal agent_duration_sum_ms=$durationTotalMs"
}

Write-Host "`n[6/6] AI cost delta + report" -ForegroundColor Yellow
$postCost = $null
try { $postCost = Invoke-JsonApi -Method GET -Path "/api/ai/cost?limit=200" } catch { Write-Warning $_.Exception.Message }
$before = if ($preCost) { To-Number $preCost.total_cost_cny } else { 0.0 }
$after = if ($postCost) { To-Number $postCost.total_cost_cny } else { 0.0 }
$delta = if ($InspectTaskId -gt 0) { 0.0 } else { [math]::Round($after - $before, 6) }
Write-Host "ai_cost_before_cny=$before ai_cost_after_cny=$after ai_cost_delta_cny=$delta"

$report = [ordered]@{
    generated_at=(Get-Date).ToString("o"); base_url=$BaseUrl
    openapi_version=$openapi.info.version; health=$health; doctor=$doctor; task=$task
    rebuild_summary=$summary; rebuild_failures=$analysisFailures; opportunity_cards=$cardRows
    agent_runs=$runRows; agent_failures=$agentFailures
    ai_cost_before_cny=$before; ai_cost_after_cny=$after; ai_cost_delta_cny=$delta
}
$reportPath = Join-Path $env:TEMP ("digital-goods-real-rebuild-{0}.json" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$report | ConvertTo-Json -Depth 30 | Set-Content -Path $reportPath -Encoding UTF8
Write-Host "REPORT_FILE=$reportPath" -ForegroundColor Green
if ($task.status -eq "FAILED") { exit 2 }
