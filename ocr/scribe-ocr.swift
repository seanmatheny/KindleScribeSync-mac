// scribe-ocr: handwriting recognition for Kindle Scribe notebook PDFs.
//
//   scribe-ocr <pdf> [--languages en-US,en-GB] [--dpi 96] [--keep-template]
//
// Prints one JSON document on stdout: the text fragments Apple's Vision framework
// found on each page, each with its best reading, the alternate readings, and a
// normalised top-left-origin bounding box. Failures print
// {"error": {"code": ..., "message": ...}} and exit non-zero (64 for usage errors).
//
// Vision runs entirely on-device. It is a separate program rather than a Python
// binding so the recognition models are unloaded again as soon as a run finishes
// instead of staying resident in the sync daemon. Build with ocr/build.sh.

import CoreGraphics
import Foundation
import PDFKit
import Vision

func writeJSON(_ object: Any) {
    let data = (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])) ?? Data("{}".utf8)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

func fail(_ code: String, _ message: String, status: Int32 = 1) -> Never {
    writeJSON(["error": ["code": code, "message": message]])
    exit(status)
}

let usage = "usage: scribe-ocr <pdf> [--languages en-US,en-GB] [--dpi 96] [--keep-template]"

/// Rasterise a PDF page. Kindle Scribe exports are 96 dpi page images wrapped in a
/// PDF, so rendering at 96 dpi reproduces the original pixels exactly.
func renderPage(_ page: PDFPage, dpi: CGFloat, stripTemplate: Bool) -> CGImage? {
    let bounds = page.bounds(for: .mediaBox)
    let scale = dpi / 72.0
    let width = Int((bounds.width * scale).rounded())
    let height = Int((bounds.height * scale).rounded())
    guard width > 0, height > 0,
          let context = CGContext(
              data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
              space: CGColorSpaceCreateDeviceRGB(),
              bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
    else { return nil }

    context.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    context.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: context)

    if stripTemplate, let data = context.data {
        // The Scribe draws its page template (rules, grids, dots) in mid grey and
        // highlighter in pale yellow; both cut through handwriting and cost the
        // recogniser whole lines. Ink is black or a saturated colour, so anything
        // light and unsaturated is safe to blank out.
        let bytesPerRow = context.bytesPerRow
        let pixels = data.bindMemory(to: UInt8.self, capacity: bytesPerRow * height)
        for row in 0..<height {
            var offset = row * bytesPerRow
            for _ in 0..<width {
                let red = Int(pixels[offset]), green = Int(pixels[offset + 1]), blue = Int(pixels[offset + 2])
                let high = max(red, green, blue), low = min(red, green, blue)
                let luminance = (2126 * red + 7152 * green + 722 * blue) / 10000
                if luminance >= 90 && (high - low) * 2 <= high {
                    pixels[offset] = 255
                    pixels[offset + 1] = 255
                    pixels[offset + 2] = 255
                }
                offset += 4
            }
        }
    }
    return context.makeImage()
}

func recogniseText(in image: CGImage, languages: [String]) throws -> [[String: Any]] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = languages
    request.customWords = ["TODO"]
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])

    return (request.results ?? []).compactMap { observation in
        // Vision's runners-up are reported too: it scores "TODD:" and "TODO:" alike and
        // often ranks the wrong one first, so the caller gets to pick between them.
        let candidates = observation.topCandidates(10)
        guard let best = candidates.first else { return nil }
        // Vision boxes are normalised with a bottom-left origin; report top-left.
        let box = observation.boundingBox
        return [
            "text": best.string,
            "alternates": candidates.dropFirst().map(\.string),
            "confidence": Double(best.confidence),
            "x": Double(box.minX),
            "y": Double(1 - box.maxY),
            "w": Double(box.width),
            "h": Double(box.height),
        ]
    }
}

// MARK: - Entry point

var path: String?
var languages = ["en-US"]
var dpi: CGFloat = 96
var stripTemplate = true

var remaining = Array(CommandLine.arguments.dropFirst())
while !remaining.isEmpty {
    let argument = remaining.removeFirst()
    switch argument {
    case "--keep-template":
        stripTemplate = false
    case "--languages", "--dpi":
        guard !remaining.isEmpty else { fail("usage", "missing value for \(argument)", status: 64) }
        let value = remaining.removeFirst()
        if argument == "--dpi" {
            guard let parsed = Double(value), parsed > 0 else { fail("usage", "invalid --dpi: \(value)", status: 64) }
            dpi = CGFloat(parsed)
        } else {
            languages = value.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        }
    default:
        guard !argument.hasPrefix("--"), path == nil else { fail("usage", usage, status: 64) }
        path = argument
    }
}

guard let path, !languages.isEmpty else { fail("usage", usage, status: 64) }
guard let document = PDFDocument(url: URL(fileURLWithPath: path)) else {
    fail("pdf_open_failed", "Could not open PDF: \(path)")
}

var pages: [[String: Any]] = []
for index in 0..<document.pageCount {
    // Each page's bitmap is ~18 MB; release it before rendering the next one.
    autoreleasepool {
        guard let page = document.page(at: index),
              let image = renderPage(page, dpi: dpi, stripTemplate: stripTemplate)
        else { fail("render_failed", "Could not render page \(index + 1) of \(path)") }
        do {
            pages.append([
                "page": index + 1,
                "width": image.width,
                "height": image.height,
                "observations": try recogniseText(in: image, languages: languages),
            ])
        } catch {
            fail("ocr_failed", "Text recognition failed on page \(index + 1): \(error.localizedDescription)")
        }
    }
}
writeJSON(["engine": "vision-text-accurate", "languages": languages, "pages": pages])
