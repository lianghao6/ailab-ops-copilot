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
    const displays = [{width:1440,height:900},{width:390,height:844}];
    for (const {display,zoom} of displays.flatMap(display=>[1,2].map(zoom=>({display,zoom})))) {
      // Halve the CSS layout viewport at 200%, then render at twice the pixel
      // density. Pixel density alone would not trigger responsive reflow.
      const viewport = {width:display.width/zoom,height:display.height/zoom,deviceScaleFactor:zoom};
      const prefix = `${display.width}${zoom===2 ? "-200pct" : ""}`;
      const page = await browser.newPage();
      await page.setViewport(viewport);
      await page.evaluateOnNewDocument(()=>localStorage.removeItem("ailab-ops-v2-context"));
      const errors=[];
      page.on("pageerror", error=>errors.push(error.message));
      page.on("console", message=>{if(message.type()==="error") errors.push(message.text());});
      // Hold the real create request to capture the native browser waiting state.
      await page.setRequestInterception(true);
      let held=null;
      let holdRead=false, heldRead=null;
      page.on("request", request=>{
        if(request.method()==="POST" && request.url()===base+"/v2/investigations") held=request;
        else if(holdRead && request.method()==="GET" && /\/v2\/investigations\/[^/?]+\?/.test(request.url())) {
          holdRead=false; heldRead=request;
        }
        else request.continue();
      });
      await page.goto(base, {waitUntil:"networkidle0"});
      await page.waitForFunction(()=>window.AILabWorkspace.state.modelMode==="replay");
      assert.strictEqual(await page.evaluate(()=>window.AILabWorkspace.state.detail),null,"intake uses clean browser context");
      const tabCycle=async name=>{
        const identify=()=>page.evaluate(()=>{
          const e=document.activeElement;
          if(e===document.body) return {name:"browser-boundary"};
          const name=e.id || e.dataset.confirm || e.dataset.action || e.dataset.reason ||
            (e.classList.contains("skip-link") ? "skip-link" : e.classList.contains("wordmark") ? "wordmark" :
              `${e.tagName}:${e.getAttribute("href") || e.textContent.trim().slice(0,24)}`);
          return {name,outline:getComputedStyle(e).outlineStyle,visible:e.getClientRects().length>0,disabled:e.disabled===true};
        });
        await page.focus(".skip-link");
        const forward=["skip-link"];
        for(let step=0;step<500;step++) {
          await page.keyboard.press("Tab");
          const target=await identify();
          if(target.name==="browser-boundary") continue;
          assert(target.visible && !target.disabled && target.outline==="solid",`${name} keyboard focus visible: ${target.name}`);
          if(target.name==="skip-link") break;
          forward.push(target.name);
          assert(step<499,`${name} Tab must leave controls and complete a cycle`);
        }
        assert.deepStrictEqual(forward.slice(0,7),["skip-link","wordmark","tenant-id","case-select","question","start-button","SUMMARY:恢复已有调查"]);
        if(name==="completed") assert.deepStrictEqual(forward.filter(id=>id.startsWith("proposal-")),[
          "proposal-tool","proposal-arguments","proposal-evidence","proposal-reason","proposal-risk","proposal-rollback","proposal-button"]);
        if(name==="pending") {
          const review=forward.filter(id=>id==="approve" || id==="reject" || id.startsWith("reject-"));
          assert.deepStrictEqual(review,["approve","approve",review[2],"reject"]);
          assert(review[2].startsWith("reject-"));
          assert(forward.includes("action-actor"));
        }
        const reverse=[];
        for(let step=0;step<500;step++) {
          await page.keyboard.down("Shift");
          await page.keyboard.press("Tab");
          await page.keyboard.up("Shift");
          const target=await identify();
          if(target.name==="browser-boundary") continue;
          assert(target.visible && !target.disabled && target.outline==="solid",`${name} reverse keyboard focus visible: ${target.name}`);
          reverse.push(target.name);
          if(target.name==="skip-link") break;
          assert(step<499,`${name} Shift+Tab must complete a cycle`);
        }
        assert.deepStrictEqual(reverse,forward.slice(1).reverse().concat("skip-link"),`${name} reverse order/no focus trap`);
        findings.push({display,zoom,tabState:name,forward,reverse,noFocusTrap:true});
      };
      const capture=async name=>{
        await page.evaluate(()=>window.scrollTo(0,0));
        await page.screenshot({path:`${output}/${prefix}-${name}.png`,fullPage:true});
        await page.screenshot({path:`${output}/${prefix}-${name}-viewport.png`});
        if (["pending","rejected","executed"].includes(name)) await (await page.$("#action-panel")).screenshot({path:`${output}/${prefix}-${name}-action.png`});
        if (["completed","insufficient"].includes(name)) await (await page.$("#cited-report")).screenshot({path:`${output}/${prefix}-${name}-report.png`});
        const measurement=await page.evaluate(()=>({
          viewport:innerWidth,scroll:document.documentElement.scrollWidth,
          h1:document.querySelectorAll("h1").length,
          mode:document.getElementById("mode-banner").textContent.trim(),
          status:document.getElementById("workspace-status").textContent.trim(),
          phase:window.AILabWorkspace.state.detail?.phase || "intake",
          overflow:[...document.querySelectorAll("body *")].filter(e=>e.getClientRects().length && e.getBoundingClientRect().right>innerWidth+1)
            .slice(0,10).map(e=>({tag:e.tagName,id:e.id,class:e.className,right:e.getBoundingClientRect().right})),
        }));
        findings.push({display,zoom,state:name,...measurement,consoleErrors:[...errors]});
        console.log(JSON.stringify(findings[findings.length-1]));
        assert(measurement.scroll<=viewport.width+1,`${viewport.width} ${name} page horizontal overflow`);
        assert.strictEqual(measurement.h1,1);
        assert.strictEqual(errors.length,0, "browser console or script error");
      };
      await capture("intake");
      assert(await page.$eval("h1",e=>e.getBoundingClientRect().left)>=24,"intake retains a readable viewport gutter");
      await tabCycle("intake");
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
      const metadata = await page.evaluate(()=>{
        const events=window.AILabWorkspace.state.detail.timeline.events;
        const rows=[...document.querySelectorAll("#timeline-list > li")];
        return events.map((event,index)=>({tool:event.tool,model:event.model,evidence:event.evidence_ids,
          text:rows[index].textContent,links:[...rows[index].querySelectorAll(".citation")].map(e=>e.getAttribute("href"))}));
      });
      assert(metadata.some(e=>e.tool) && metadata.some(e=>e.model),"replay has real tool and model calls");
      for(const event of metadata) {
        if(event.tool) assert(event.text.includes(`工具 / ${event.tool}`));
        if(event.model) assert(event.text.includes(`模型 / ${event.model}`));
        for(const id of event.evidence) assert(event.text.includes(id) && event.links.length,"linked call evidence");
        for(const field of ["usage","latency_ms","retry","error"]) assert(event.text.includes(field),`call metadata ${field}`);
      }
      findings.push({display,zoom,timelineMetadata:true,toolCalls:metadata.filter(e=>e.tool).length,modelCalls:metadata.filter(e=>e.model).length});
      await tabCycle("completed");
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
      // Cross the shipped 2500ms timer, not a manually invoked refresh. Hold a
      // real background detail GET to inspect controls before and after it.
      for(const selector of ["#timeline-list summary", "[data-action=approve]"]) {
        await page.evaluate(()=>{
          const panels=["evidence-list","report-content","timeline-list","approval-audit"];
          window.__qaReading=panels.map(id=>document.getElementById(id).firstElementChild);
          window.__qaDisclosures=[...document.querySelectorAll("#evidence-list details,#timeline-list details,#approval-audit details")];
          window.__qaDisclosures.forEach(e=>{e.open=true;});
          window.__qaPollRendered=false;
          document.addEventListener("ailab:investigation",()=>{window.__qaPollRendered=true;},{once:true});
        });
        await page.focus(selector);
        const requested=page.waitForRequest(request=>request.method()==="GET" && /\/v2\/investigations\/[^/?]+\?/.test(request.url()),{timeout:10000});
        holdRead=true;
        await requested;
        assert(heldRead,"automatic detail GET held");
        const check=()=>page.evaluate(selector=>({
          focus:document.activeElement===document.querySelector(selector),
          available:!document.querySelector("[data-action=approve]").disabled,
          busy:window.AILabWorkspace.state.busy,
          retained:["evidence-list","report-content","timeline-list","approval-audit"].every((id,index)=>document.getElementById(id).firstElementChild===window.__qaReading[index]),
          open:window.__qaDisclosures.every(e=>e.isConnected && e.open),
        }),selector);
        const during=await check();
        assert(during.focus && during.available && !during.busy && during.retained && during.open,"poll in flight does not interrupt reading/focus/approval");
        await heldRead.continue(); heldRead=null;
        await page.waitForFunction(()=>window.__qaPollRendered);
        const after=await check();
        assert(after.focus && after.available && !after.busy && after.retained && after.open,"poll completion retains reading/focus/approval");
        findings.push({display,zoom,pollFocus:selector,timerMs:2500,during,after});
      }
      // Restore disclosure defaults so keyboard traversal uses the same state
      // as the earlier QA baseline.
      await page.evaluate(()=>window.__qaDisclosures.forEach(e=>{e.open=false;}));
      await tabCycle("pending");
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
      await page.focus("#start-button");
      await page.keyboard.press("Enter");
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
      findings.push({display,zoom,viewport,stress,reducedMotion:true,keyboard:{skip:true,disclosure:true,proposal:true,reject:true,approve:true,execute:true}});
      await page.close();
    }
  } finally {
    fs.writeFileSync(output+"/findings.json",JSON.stringify(findings,null,2));
    await browser.close();
  }
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
