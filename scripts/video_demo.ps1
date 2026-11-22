# Terminal part of the demo video (Windows PowerShell).
#   cd C:\dev\tuition-assistant ; .\.venv\Scripts\Activate.ps1 ; .\scripts\video_demo.ps1
# Needs: local PostgreSQL for the unit tests (docker compose up -d db) and the Azure deployment.
# Press Enter between sections.

$GW  = "https://gateway.wittydune-e15fb50a.swedencentral.azurecontainerapps.io"
$API = "https://tuition-api.wittydune-e15fb50a.swedencentral.azurecontainerapps.io"
$env:PYTHONUTF8 = "1"

function Title($text) {
    Write-Host ""
    Write-Host ("=" * 78) -ForegroundColor Cyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host ("=" * 78) -ForegroundColor Cyan
}
function Next { Read-Host "`n[Enter] next section" | Out-Null }
function Call($label, $url, $headers = @{}) {
    Write-Host "`n> $label" -ForegroundColor Yellow
    Write-Host "  GET $url" -ForegroundColor DarkGray
    try {
        $r = Invoke-WebRequest $url -Headers $headers -UseBasicParsing
        Write-Host "  HTTP $($r.StatusCode)" -ForegroundColor Green
        Write-Host "  $($r.Content)"
    } catch {
        Write-Host "  HTTP $($_.Exception.Response.StatusCode.value__)" -ForegroundColor Red
        Write-Host "  $($_.ErrorDetails.Message)"
    }
}

Title "1. Unit tests  (pytest: local PostgreSQL only - no cloud, no LLM)"
Write-Host "> pytest -q" -ForegroundColor Yellow
pytest -q
Next

Title "2. Three MCP retrieval tests  (client -> MCP protocol -> search_tuition_policies)"
Write-Host "> python -m mcp_server.demo_queries" -ForegroundColor Yellow
python -m mcp_server.demo_queries 2>$null
Next

Title "3. API Gateway on Azure - protected route  ($GW)"
Call "Protected route WITHOUT a token" "$GW/api/v1/banking/tuition/20210001"
Write-Host "`n> POST $GW/api/v1/auth/token  (student 20210001)" -ForegroundColor Yellow
$token = (Invoke-RestMethod "$GW/api/v1/auth/token" -Method Post -ContentType "application/json" `
          -Body '{"username":"20210001","password":"student123"}').accessToken
Write-Host "  JWT received: $($token.Substring(0, 20))..." -ForegroundColor Green
Call "Protected route WITH the student's JWT" "$GW/api/v1/banking/tuition/20210001" @{ Authorization = "Bearer $token" }
Call "Same JWT on ANOTHER student's account" "$GW/api/v1/banking/tuition/20210002" @{ Authorization = "Bearer $token" }
Next

Title "4. Rate limit: mobile Query Tuition = 3 calls per student per day"
foreach ($i in 1..4) { Call "Mobile query #$i for student 20210004" "$GW/api/v1/mobile/tuition/20210004" }
Next

Title "5. The Tuition API cannot be called without the gateway"
Call "Direct call to the Tuition API (bypassing the gateway)" "$API/api/v1/mobile/tuition/20210001"

Title "End of terminal demo"
