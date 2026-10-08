"use strict";
const fs = require("fs");
const assert = require("assert");
const puppeteer = require("puppeteer-core");
const [base, executablePath, output] = process.argv.slice(2);
assert(base && executablePath && output, "Usage: node scripts/qa_v2_workspace.cjs <base_url> <browser_path> <output_dir>");
assert(["127.0.0.1", "localhost"].includes(new URL(base).hostname), "QA mutations are restricted to a local replay service");
fs.mkdirSync(output, {recursive:true});
const findings = [];

(async () => {
  const browser = await puppeteer.launch({executablePath, headless:true,
    args:["--no-sandbox","--disable-setuid-sandbox","--no-zygote","--single-process","--disable-dev-shm-usage","--no-proxy-server"],
    env:process.env});
  try {
    console.log(await browser.version());
    for (const viewport of [{width:1440,height:900},{width:390,height:844}]) {
      const page = await browser.newPage();
      await page.setViewport(viewport);
      await page.evaluateOnNewDocument(()=>localStorage.removeItem("ailab-ops-v2-context"));
      const errors=[];
      page.on("pageerror", error=>errors.push(error.message));
      page.on("console", message=>{if(message.type()==="error") errors.push(message.text());});
      // Hold the real create request to capture the native browser waiting state.
      await page.setRequestInterception(true);
      let held=null;
      page.on("request", request=>{
        if(request.method()==="POST" && request.url()===base+"/v2/investigations") held=request;
        else request.continue();
      });
      await page.goto(base, {waitUntil:"networkidle0"});
      await page.waitForFunction(()=>window.AILabWorkspace.state.modelMode==="replay");
      assert.strictEqual(await page.evaluate(()=>window.AILabWorkspace.state.detail),null,"intake uses clean browser context");
      const capture=async name=>{
        await page.evaluate(()=>window.scrollTo(0,0));
        await page.screenshot({path:`${output}/${viewport.width}-${name}.png`,fullPage:true});
        await page.screenshot({path:`${output}/${viewport.width}-${name}-viewport.png`});
        if (["pending","rejected","executed"].includes(name)) await (await page.$("#action-panel")).screenshot({path:`${output}/${viewport.width}-${name}-action.png`});
        if (["completed","insufficient"].includes(name)) await (await page.$("#cited-report")).screenshot({path:`${output}/${viewport.width}-${name}-report.png`});
        const measurement=await page.evaluate(()=>({
          viewport:innerWidth,scroll:document.documentElement.scrollWidth,
          h1:document.querySelectorAll("h1").length,
          mode:document.getElementById("mode-banner").textContent.trim(),
          status:document.getElementById("workspace-status").textContent.trim(),
          phase:window.AILabWorkspace.state.detail?.phase || "intake",
          overflow:[...document.querySelectorAll("main *")].filter(e=>e.getClientRects().length && e.getBoundingClientRect().right>innerWidth+1)
            .slice(0,10).map(e=>({tag:e.tagName,id:e.id,class:e.className,right:e.getBoundingClientRect().right})),
        }));
        findings.push({viewport,state:name,...measurement,consoleErrors:[...errors]});
        console.log(JSON.stringify(findings[findings.length-1]));
        assert(measurement.scroll<=viewport.width+1,`${viewport.width} ${name} page horizontal overflow`);
        assert.strictEqual(measurement.h1,1);
        assert.strictEqual(errors.length,0, "browser console or script error");
      };
      await capture("intake");
      assert(await page.$eval("h1",e=>e.getBoundingClientRect().left)>=24,"intake retains a readable viewport gutter");
      await page.keyboard.press("Tab");
      assert.strictEqual(await page.evaluate(()=>document.activeElement.className),"skip-link");
      assert.strictEqual(await page.evaluate(()=>getComputedStyle(document.activeElement).outlineStyle),"solid");
      await page.keyboard.press("Enter");
      assert.strictEqual(await page.evaluate(()=>document.activeElement.id),"investigation");
      await page.focus(".restore summary");
      await page.keyboard.press("Space");
      assert(await page.$eval(".restore",e=>e.open),"native restore disclosure Space");
      await page.keyboard.press("Space");
      await page.focus("#start-button");
      await page.keyboard.press("Enter");
      await page.waitForFunction(()=>window.AILabWorkspace.state.busy);
      assert.strictEqual(await page.$eval("#investigation",e=>e.getAttribute("aria-busy")),"true");
      await capture("investigating");
      assert(held,"held real create request exists");
      await held.continue(); held=null;
      await page.waitForFunction(()=>window.AILabWorkspace.state.detail?.phase==="completed" && !window.AILabWorkspace.state.busy);
      await capture("completed");
      const submit=async ()=>{
        await page.evaluate(()=>{
          const fields={"proposal-tool":"annotate_incident","proposal-arguments":JSON.stringify({case_id:"case-gpu-assert",note:"Browser QA"}),
            "proposal-reason":"Verify cited finding","proposal-risk":"Wrong annotation","proposal-rollback":"Remove note",
            "proposal-evidence":window.AILabWorkspace.state.detail.evidence_ids.join(",")};
          for(const [id,value] of Object.entries(fields)) document.getElementById(id).value=value;
        });
        await page.focus("#proposal-button"); await page.keyboard.press("Enter");
        await page.waitForFunction(()=>window.AILabWorkspace.state.detail.approvals.at(-1)?.status==="pending" && !window.AILabWorkspace.state.busy);
      };
      await submit();
      await capture("pending");
      assert.strictEqual(await page.$("[data-action=execute]"),null);
      assert(await page.$eval("#approval-list details",e=>e.open));
      await page.$eval("#action-actor",e=>{e.value="browser-reviewer";});
      await page.$eval("[data-reason=reject]",e=>{e.value="Browser QA rejection";});
      await page.focus("[data-action=reject]"); await page.keyboard.press("Enter");
      await page.waitForFunction(()=>window.AILabWorkspace.state.detail.approvals.at(-1)?.status==="rejected" && !window.AILabWorkspace.state.busy);
      await capture("rejected");
      await submit();
      await page.focus("[data-confirm=approve]"); await page.keyboard.press("Space");
      assert(await page.$eval("[data-confirm=approve]",e=>e.checked));
      await page.focus("[data-action=approve]"); await page.keyboard.press("Enter");
      await page.waitForFunction(()=>window.AILabWorkspace.state.detail.approvals.at(-1)?.status==="approved" && !window.AILabWorkspace.state.busy);
      assert.strictEqual(await page.$eval("[data-confirm=execute]",e=>e.checked),false);
      await page.focus("[data-confirm=execute]"); await page.keyboard.press("Space");
      await page.focus("[data-action=execute]"); await page.keyboard.press("Enter");
      await page.waitForFunction(()=>window.AILabWorkspace.state.detail.approvals.at(-1)?.status==="executed" && !window.AILabWorkspace.state.busy);
      await page.evaluate(()=>{ const summary=[...document.querySelectorAll("#approval-list summary")].find(e=>e.textContent.includes("模拟执行结果")); summary.parentElement.open=true; });
      await capture("executed");
      assert(await page.$eval("#approval-list",e=>e.textContent.includes("模拟执行结果")));
      assert.strictEqual(await page.$("[data-action=execute]"),null);
      await page.select("#case-select","case-insufficient-evidence");
      await page.click("#start-button");
      await page.waitForFunction(()=>window.AILabWorkspace.state.busy);
      await held.continue(); held=null;
      await page.waitForFunction(()=>window.AILabWorkspace.state.detail?.case_id==="case-insufficient-evidence" && !window.AILabWorkspace.state.busy);
      await capture("insufficient");
      assert(await page.$eval("#report-content",e=>e.textContent.includes("insufficient_evidence")));
      await page.emulateMediaFeatures([{name:"prefers-reduced-motion",value:"reduce"}]);
      assert.strictEqual(await page.evaluate(()=>getComputedStyle(document.documentElement).scrollBehavior),"auto");
      const stress=await page.evaluate(()=>{
        const d=JSON.parse(JSON.stringify(window.AILabWorkspace.state.detail));
        const long="very-long-unbroken-evidence-".repeat(80);
        d.question=long; d.plan=[long]; d.report.summary=long; d.report.unknowns=[long]; d.evidence[0].excerpt=long;
        window.AILabWorkspace.render(d);
        return {viewport:innerWidth,scroll:document.documentElement.scrollWidth};
      });
      assert(stress.scroll<=viewport.width+1,"long text horizontal overflow");
      findings.push({viewport,stress,reducedMotion:true,keyboard:{skip:true,disclosure:true,proposal:true,reject:true,approve:true,execute:true}});
      await page.close();
    }
  } finally {
    fs.writeFileSync(output+"/findings.json",JSON.stringify(findings,null,2));
    await browser.close();
  }
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
