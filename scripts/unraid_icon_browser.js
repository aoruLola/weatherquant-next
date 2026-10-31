// Run in DevTools on the signed-in Unraid Docker tab after the page settles.
// Produces a small JSON report without cookies or request headers.
(async () => {
    const path = "/plugins/dynamix.docker.manager/images/question.png";
    if (location.pathname.replace(/\/$/, "") !== "/Docker" || document.hidden
        || !document.querySelector("#docker_containers #docker_list")) {
        throw new Error("Open the visible Unraid Docker tab first");
    }
    const response = await fetch(path, {cache: "no-store", redirect: "error"});
    if (response.status !== 200 || response.headers.get("content-type")?.split(";")[0] !== "image/png") {
        throw new Error("The authenticated request did not return HTTP 200 image/png");
    }
    const image = await response.blob();
    const bitmap = await createImageBitmap(image);
    bitmap.close();
    const png = new Uint8Array(await image.arrayBuffer());
    let encoded = "";
    for (const byte of png) encoded += String.fromCharCode(byte);

    let requests = 0;
    let dropped = 0;
    let hidden = false;
    const count = entries => {
        for (const entry of entries) {
            if (entry.startTime >= since && new URL(entry.name).pathname === path) requests++;
        }
    };
    const since = performance.now();
    const start = Date.now() / 1000;
    const visibility = () => { if (document.hidden) hidden = true; };
    document.addEventListener("visibilitychange", visibility);
    const observer = new PerformanceObserver((list, _, options) => {
        count(list.getEntries());
        dropped += options?.droppedEntriesCount || 0;
    });
    observer.observe({type: "resource"});
    await new Promise(resolve => setTimeout(resolve, 61000));
    count(observer.takeRecords());
    observer.disconnect();
    document.removeEventListener("visibilitychange", visibility);
    const report = {origin: location.origin, page: location.pathname, start, end: Date.now() / 1000,
        hidden, dropped, requests, image_base64: btoa(encoded)};
    console.log(JSON.stringify(report));
    return report;
})();
