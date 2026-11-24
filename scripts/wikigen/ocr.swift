// Recognize the text in PNG files with Apple's Vision framework.
// Usage: swift ocr.swift <png>...   Prints {"<path>": "<text, lines joined by \n>"} to stdout.
import Foundation
import ImageIO
import Vision

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

var result: [String: String] = [:]
for path in CommandLine.arguments.dropFirst() {
    let url = URL(fileURLWithPath: path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
        let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
    else { fail("cannot read image: \(path)") }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    do {
        try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    } catch {
        fail("recognition failed for \(path): \(error)")
    }
    let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
    result[path] = lines.joined(separator: "\n")
}
do {
    let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
} catch {
    fail("cannot encode result: \(error)")
}
