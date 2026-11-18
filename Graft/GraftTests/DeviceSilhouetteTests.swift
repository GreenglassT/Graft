import SwiftUI
import XCTest
@testable import Graft

/// `DeviceSilhouette` draws from a classification, `Face`, rather than the
/// product-type string directly — see that type's doc comment for the
/// model-number research behind each boundary. Asserting on a rendered
/// SwiftUI view is expensive and brittle, so these check the classification
/// only, the same approach `AppIconTests` takes for `AppIcon`.
@MainActor
final class DeviceSilhouetteTests: XCTestCase {

    // MARK: - The cases from the plan

    func test_product_types_map_to_the_right_face() {
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone16,1"), .dynamicIsland, "iPhone 15 Pro")
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone14,2"), .notch, "iPhone 13 Pro")
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone8,1"), .homeButton, "iPhone 6s")
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPad13,1"), .generic, "not an iPhone at all")
        XCTAssertEqual(DeviceSilhouette.Face(productType: "who-knows"), .generic)
    }

    // MARK: - Malformed and edge-case input

    func test_empty_string_is_generic() {
        XCTAssertEqual(DeviceSilhouette.Face(productType: ""), .generic)
    }

    func test_iPhone_with_no_numbers_is_generic() {
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone,"), .generic)
    }

    func test_future_model_parses_to_the_newest_face_not_generic() {
        // The pattern parses even though nothing has assigned this
        // identifier yet — a future phone should read as "the newest thing
        // we know how to draw," not as unrecognised.
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone999,1"), .dynamicIsland)
    }

    // MARK: - The iPhone X generation: notch and home button share a major number

    func test_iPhone_X_variants_are_notch() {
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone10,3"), .notch, "iPhone X Global — the first notch")
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone10,6"), .notch, "iPhone X GSM")
    }

    func test_iPhone_8_variants_sharing_major_10_are_still_home_button() {
        // iPhone 8 and 8 Plus shipped the same year as the X, under the same
        // major number (10), but kept the home button. Minor number, not a
        // rising major, is what separates them from 10,3/10,6.
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone10,1"), .homeButton, "iPhone 8")
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone10,2"), .homeButton, "iPhone 8 Plus")
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone10,4"), .homeButton, "iPhone 8, GSM variant")
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone10,5"), .homeButton, "iPhone 8 Plus, GSM variant")
    }

    // MARK: - The SE resets: a later major number, but the old body

    func test_iPhone_SE_2nd_gen_is_home_button_despite_major_number_12() {
        // Launched alongside the notched iPhone 11 line; major number 12
        // would otherwise read as notch.
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone12,8"), .homeButton)
    }

    func test_iPhone_SE_3rd_gen_is_home_button_despite_major_number_14() {
        // Launched alongside the notched iPhone 13 line; major number 14
        // would otherwise read as notch.
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone14,6"), .homeButton)
    }

    func test_iPhone_SE_1st_gen_is_home_button() {
        // Doesn't need the exception list — major 8 is under the notch
        // boundary on its own — but worth pinning down explicitly.
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone8,4"), .homeButton)
    }

    // MARK: - The 14 lineup: Pro jumped a major number, non-Pro didn't

    func test_iPhone_14_non_pro_is_notch_not_dynamic_island() {
        // iPhone 14 and 14 Plus launched beside the Dynamic Island 14 Pro
        // models but kept major number 14, one behind the Pro's 15.
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone14,7"), .notch, "iPhone 14")
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone14,8"), .notch, "iPhone 14 Plus")
    }

    func test_iPhone_14_pro_variants_are_the_first_dynamic_island() {
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone15,2"), .dynamicIsland, "iPhone 14 Pro")
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone15,3"), .dynamicIsland, "iPhone 14 Pro Max")
    }

    func test_iPhone_15_non_pro_also_got_the_island() {
        // Unlike the 14 lineup, Apple gave the 15 and 15 Plus (not just the
        // Pro models) the Dynamic Island, and they share major number 15
        // with the Pro pair above.
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone15,4"), .dynamicIsland, "iPhone 15")
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone15,5"), .dynamicIsland, "iPhone 15 Plus")
    }

    // MARK: - The 16e: a notch reappearing inside the Dynamic Island era

    func test_iPhone_16e_is_notch_despite_sharing_a_major_number_with_the_16_line() {
        // iPhone17,1 through 17,4 (the 16 line) are Dynamic Island; 17,5
        // (the 16e) shipped in 2025 with a plain notch regardless.
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone17,5"), .notch)
    }

    func test_rest_of_the_16_line_is_dynamic_island() {
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone17,1"), .dynamicIsland, "iPhone 16 Pro")
        XCTAssertEqual(DeviceSilhouette.Face(productType: "iPhone17,3"), .dynamicIsland, "iPhone 16")
    }
}
