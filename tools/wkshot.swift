// Render a page in WebKit, the iPad's engine, off screen, and measure it.
//
//   swift tools/wkshot.swift PAGE.html WIDTH HEIGHT [OUT.png|-] [SETUP.js]
//
// Loads PAGE.html (a file: the web app needs no server) in a WIDTH x HEIGHT
// WKWebView that is never put on screen.  Once the page has loaded, it runs
// SETUP.js and prints JSON.stringify(window.__measure()) if the setup defined
// one.  Then it writes a 2x snapshot to OUT.png unless OUT is "-".
//
// There is no hub here, so WebSocket is stubbed before the page's own script
// runs.  Uncaught errors are collected in window.__errors for __measure to
// return.  requestAnimationFrame never fires off screen, so the setup must call
// directly whatever the page would have run on the next frame.
//
// Used by tools/test_web_move_layout.py, and handy on its own for a look at a
// layout in the iPad's engine.  Headless Chrome is not that engine.
//
// Exit status: 0 done, 1 bad arguments or no snapshot, 2 timed out,
// 3 the setup threw.

import Cocoa
import WebKit

let args = CommandLine.arguments
guard args.count >= 4, let W = Double(args[2]), let H = Double(args[3]) else {
    FileHandle.standardError.write("usage: wkshot PAGE.html WIDTH HEIGHT [OUT.png|-] [SETUP.js]\n".data(using: .utf8)!)
    exit(1)
}
let page  = URL(fileURLWithPath: args[1])
let out   = args.count > 4 ? args[4] : "-"
let setup = args.count > 5 ? (try? String(contentsOfFile: args[5], encoding: .utf8)) ?? "" : ""

func fail(_ code: Int32, _ what: String) -> Never {
    FileHandle.standardError.write((what + "\n").data(using: .utf8)!)
    exit(code)
}

final class Loaded: NSObject, WKNavigationDelegate {
    let setup: String, out: String
    init(setup: String, out: String) { self.setup = setup; self.out = out }

    func webView(_ v: WKWebView, didFinish _: WKNavigation!) {
        // ";0": the setup's last value need not be one WebKit can hand back.
        v.evaluateJavaScript(setup + "\n;0") { _, e in
            if let e = e { fail(3, "setup threw: \(e)") }
            v.evaluateJavaScript("window.__measure ? JSON.stringify(window.__measure()) : ''") { r, e in
                if let e = e { fail(3, "__measure threw: \(e)") }
                if let r = r as? String, !r.isEmpty { print(r) }
                if self.out == "-" { exit(0) }
                v.takeSnapshot(with: WKSnapshotConfiguration()) { img, e in
                    guard let img = img, let tiff = img.tiffRepresentation,
                          let rep = NSBitmapImageRep(data: tiff),
                          let png = rep.representation(using: .png, properties: [:]) else {
                        fail(1, "no snapshot: \(String(describing: e))")
                    }
                    do { try png.write(to: URL(fileURLWithPath: self.out)) }
                    catch { fail(1, "cannot write \(self.out): \(error)") }
                    exit(0)
                }
            }
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)            // no Dock icon, nothing on screen
let cfg = WKWebViewConfiguration()
cfg.websiteDataStore = .nonPersistent()          // the page's localStorage dies with it
cfg.userContentController.addUserScript(WKUserScript(source: """
    window.WebSocket = function () {
        return { send() {}, close() {}, readyState: 0, addEventListener() {} };
    };
    window.__errors = [];
    window.addEventListener('error', e => window.__errors.push(String(e.message) + ' @' + e.lineno));
    """, injectionTime: .atDocumentStart, forMainFrameOnly: true))
let view = WKWebView(frame: NSRect(x: 0, y: 0, width: W, height: H), configuration: cfg)
let window = NSWindow(contentRect: view.frame, styleMask: [.borderless],
                      backing: .buffered, defer: false)
window.contentView = view
let loaded = Loaded(setup: setup, out: out)
view.navigationDelegate = loaded
view.loadFileURL(page, allowingReadAccessTo: page.deletingLastPathComponent())
DispatchQueue.main.asyncAfter(deadline: .now() + 30) { fail(2, "timed out") }
app.run()
