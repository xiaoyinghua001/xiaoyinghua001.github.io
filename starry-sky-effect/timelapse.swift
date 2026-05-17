import AVFoundation
import CoreGraphics
import CoreVideo
import Foundation

struct Arguments {
    let input: String
    let output: String
    let interval: Double
    let fps: Int32
    let bitrate: Int
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

func emit(_ message: String) {
    FileHandle.standardOutput.write(Data((message + "\n").utf8))
}

func value(after flag: String, in args: [String]) -> String? {
    guard let index = args.firstIndex(of: flag), index + 1 < args.count else { return nil }
    return args[index + 1]
}

func parseArguments() -> Arguments {
    let args = Array(CommandLine.arguments.dropFirst())
    guard
        let input = value(after: "--input", in: args),
        let output = value(after: "--output", in: args),
        let intervalText = value(after: "--interval", in: args),
        let fpsText = value(after: "--fps", in: args),
        let bitrateText = value(after: "--bitrate", in: args),
        let interval = Double(intervalText),
        let fps = Int32(fpsText),
        let bitrate = Int(bitrateText)
    else {
        fail("参数不完整")
    }

    return Arguments(
        input: input,
        output: output,
        interval: max(0.1, interval),
        fps: max(1, fps),
        bitrate: max(500_000, bitrate)
    )
}

func waitUntilReady(_ input: AVAssetWriterInput) {
    while !input.isReadyForMoreMediaData {
        Thread.sleep(forTimeInterval: 0.01)
    }
}

func draw(_ image: CGImage, into pixelBuffer: CVPixelBuffer, width: Int, height: Int) {
    CVPixelBufferLockBaseAddress(pixelBuffer, [])
    defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, []) }

    guard
        let baseAddress = CVPixelBufferGetBaseAddress(pixelBuffer),
        let context = CGContext(
            data: baseAddress,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: CVPixelBufferGetBytesPerRow(pixelBuffer),
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue
        )
    else {
        fail("无法创建视频帧缓冲区")
    }

    context.setFillColor(CGColor(gray: 0, alpha: 1))
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))

    let imageWidth = CGFloat(image.width)
    let imageHeight = CGFloat(image.height)
    let targetWidth = CGFloat(width)
    let targetHeight = CGFloat(height)
    let scale = min(targetWidth / imageWidth, targetHeight / imageHeight)
    let drawWidth = imageWidth * scale
    let drawHeight = imageHeight * scale
    let drawRect = CGRect(
        x: (targetWidth - drawWidth) / 2,
        y: (targetHeight - drawHeight) / 2,
        width: drawWidth,
        height: drawHeight
    )

    context.interpolationQuality = .high
    context.draw(image, in: drawRect)
}

let arguments = parseArguments()
let sourceURL = URL(fileURLWithPath: arguments.input)
let outputURL = URL(fileURLWithPath: arguments.output)

try? FileManager.default.removeItem(at: outputURL)
try? FileManager.default.createDirectory(
    at: outputURL.deletingLastPathComponent(),
    withIntermediateDirectories: true
)

let asset = AVAsset(url: sourceURL)
let duration = CMTimeGetSeconds(asset.duration)
guard duration.isFinite, duration > 0 else {
    fail("无法读取视频时长")
}

guard let videoTrack = asset.tracks(withMediaType: .video).first else {
    fail("找不到视频轨道")
}

let naturalSize = videoTrack.naturalSize.applying(videoTrack.preferredTransform)
let rawWidth = max(2, Int(abs(naturalSize.width)))
let rawHeight = max(2, Int(abs(naturalSize.height)))
let width = rawWidth - (rawWidth % 2)
let height = rawHeight - (rawHeight % 2)

let frameCount = max(1, Int(floor(duration / arguments.interval)) + 1)
let outputFrameDuration = CMTime(value: 1, timescale: arguments.fps)

let writer: AVAssetWriter
do {
    writer = try AVAssetWriter(outputURL: outputURL, fileType: .mp4)
} catch {
    fail("无法创建 MP4 文件：\(error.localizedDescription)")
}

let compression: [String: Any] = [
    AVVideoAverageBitRateKey: arguments.bitrate,
    AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
    AVVideoMaxKeyFrameIntervalKey: Int(arguments.fps * 2)
]

let outputSettings: [String: Any] = [
    AVVideoCodecKey: AVVideoCodecType.h264,
    AVVideoWidthKey: width,
    AVVideoHeightKey: height,
    AVVideoCompressionPropertiesKey: compression
]

let input = AVAssetWriterInput(mediaType: .video, outputSettings: outputSettings)
input.expectsMediaDataInRealTime = false

let adaptor = AVAssetWriterInputPixelBufferAdaptor(
    assetWriterInput: input,
    sourcePixelBufferAttributes: [
        kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB,
        kCVPixelBufferWidthKey as String: width,
        kCVPixelBufferHeightKey as String: height,
        kCVPixelBufferCGImageCompatibilityKey as String: true,
        kCVPixelBufferCGBitmapContextCompatibilityKey as String: true
    ]
)

guard writer.canAdd(input) else {
    fail("无法添加视频轨道")
}
writer.add(input)

let generator = AVAssetImageGenerator(asset: asset)
generator.appliesPreferredTrackTransform = true
generator.requestedTimeToleranceBefore = .zero
generator.requestedTimeToleranceAfter = .zero
generator.maximumSize = CGSize(width: width, height: height)

guard writer.startWriting() else {
    fail("开始写入失败：\(writer.error?.localizedDescription ?? "未知错误")")
}
writer.startSession(atSourceTime: .zero)

for index in 0..<frameCount {
    autoreleasepool {
        let sourceSeconds = min(Double(index) * arguments.interval, max(0, duration - 0.04))
        let sourceTime = CMTime(seconds: sourceSeconds, preferredTimescale: 600)
        let outputTime = CMTimeMultiply(outputFrameDuration, multiplier: Int32(index))

        waitUntilReady(input)

        guard let pool = adaptor.pixelBufferPool else {
            fail("无法创建像素缓冲池")
        }

        var maybeBuffer: CVPixelBuffer?
        let status = CVPixelBufferPoolCreatePixelBuffer(nil, pool, &maybeBuffer)
        guard status == kCVReturnSuccess, let pixelBuffer = maybeBuffer else {
            fail("无法创建像素缓冲")
        }

        do {
            let cgImage = try generator.copyCGImage(at: sourceTime, actualTime: nil)
            draw(cgImage, into: pixelBuffer, width: width, height: height)
        } catch {
            fail("抽取第 \(index + 1) 帧失败：\(error.localizedDescription)")
        }

        guard adaptor.append(pixelBuffer, withPresentationTime: outputTime) else {
            fail("写入第 \(index + 1) 帧失败：\(writer.error?.localizedDescription ?? "未知错误")")
        }

        let progress = Int(round((Double(index + 1) / Double(frameCount)) * 100))
        emit("PROGRESS \(progress)")
    }
}

input.markAsFinished()

let group = DispatchGroup()
group.enter()
writer.finishWriting {
    group.leave()
}
group.wait()

if writer.status != .completed {
    fail("MP4 完成失败：\(writer.error?.localizedDescription ?? "未知错误")")
}

emit("DONE \(outputURL.path)")
