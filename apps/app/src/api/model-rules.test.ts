import { describe, expect, it } from "vitest";

import { acceptedClasses, endpointHint, parseRegions, reasonProblem, regionsWiden, splitRef, validProcessing, widens } from "./model-rules";

describe("reason (3 to 500 characters after trimming, as the server counts)", () => {
  it.each([["", false], ["  ", false], ["ab", false], ["  ab  ", false], ["abc", true], ["  abc ", true], ["x".repeat(500), true], ["x".repeat(501), false], ["é".repeat(3), true]])(
    "%j is acceptable: %s",
    (reason, ok) => {
      expect(reasonProblem(reason) === null).toBe(ok);
    },
  );
  it("counts a surrogate pair as one character", () => {
    expect(reasonProblem("😀😀")).not.toBeNull();
    expect(reasonProblem("😀😀😀")).toBeNull();
  });
});

describe("data classes (ND45)", () => {
  it("global takes synthetic data only; canada both; a region only when chosen", () => {
    expect(acceptedClasses("global", [])).toEqual(["synthetic"]);
    expect(acceptedClasses("canada", [])).toEqual(["synthetic", "customer"]);
    expect(acceptedClasses("region:eu-west", [])).toEqual(["synthetic"]);
    expect(acceptedClasses("region:eu-west", ["eu-west"])).toEqual(["synthetic", "customer"]);
  });
  it("an edit that gains a class widens; one that keeps or loses does not", () => {
    expect(widens("global", "canada", [])).toBe(true);
    expect(widens("canada", "global", [])).toBe(false);
    expect(widens("global", "region:xy", [])).toBe(false);
    expect(widens("global", "region:xy", ["xy"])).toBe(true);
    expect(widens("canada", "canada", [])).toBe(false);
  });
  it("a regions write widens only approved region-bound entries that gain customer data", () => {
    const entries = [
      { ref: "a/eu", processing: "region:eu-west", status: "approved" },
      { ref: "a/eu2", processing: "region:eu-west", status: "paused" },
      { ref: "a/us", processing: "region:us-east", status: "approved" },
      { ref: "a/ca", processing: "canada", status: "approved" },
      { ref: "a/gl", processing: "global", status: "approved" },
    ];
    expect(regionsWiden(entries, [], ["eu-west"])).toEqual(["a/eu"]);
    expect(regionsWiden(entries, ["eu-west"], [])).toEqual([]);
    expect(regionsWiden(entries, ["eu-west"], ["eu-west", "us-east"])).toEqual(["a/us"]);
  });
});

describe("small helpers", () => {
  it("validates processing scopes", () => {
    for (const ok of ["canada", "global", "region:eu-west"]) expect(validProcessing(ok)).toBe(true);
    for (const bad of ["mars", "region:", "region:A", "Canada", ""]) expect(validProcessing(bad)).toBe(false);
  });
  it("parses a region list: sorted, de-duplicated, any separator", () => {
    expect(parseRegions("eu-west, ca-east\nca-east  us-east")).toEqual(["ca-east", "eu-west", "us-east"]);
    expect(parseRegions("  ")).toEqual([]);
  });
  it("hints about the endpoint shape without deciding", () => {
    expect(endpointHint("https://aoai-x.openai.azure.com")).toBeNull();
    expect(endpointHint("https://ais-x.cognitiveservices.azure.com/")).toBeNull();
    expect(endpointHint("http://aoai-x.openai.azure.com")).not.toBeNull();
    expect(endpointHint("https://evil.example.com")).not.toBeNull();
  });
  it("splits a ref", () => {
    expect(splitRef("azure-openai/gpt-5.4-mini")).toEqual({ provider: "azure-openai", model: "gpt-5.4-mini" });
    expect(splitRef("nope")).toBeNull();
  });
});
