import { describe, expect, it } from "vitest";
import {
  groupPresets,
  isEmbeddingLaneProvider,
  isLocalProviderRow,
  isOllamaProviderRow,
  isOllamaRuntimeUrl,
  isOnnxProviderRow,
  looksLikeOllamaProvider,
  splitPinnedPresets,
} from "./presetUtils";

describe("isOnnxProviderRow", () => {
  it("matches the onboard ONNX placeholder key or exact name", () => {
    expect(isOnnxProviderRow({ name: "anything", api_key: "onnx" })).toBe(true);
    expect(isOnnxProviderRow({ name: "ONNX (Local)" })).toBe(true);
    expect(isOnnxProviderRow({ name: "  ONNX (Local)  " })).toBe(true);
    expect(
      isOnnxProviderRow({ name: "My ONNX Cloud Proxy", api_key: "sk-x" }),
    ).toBe(false);
  });
});

describe("isEmbeddingLaneProvider", () => {
  it("includes ONNX, purpose=embedding, and all-embedding model lists", () => {
    expect(isEmbeddingLaneProvider({ name: "ONNX (Local)" })).toBe(true);
    expect(
      isEmbeddingLaneProvider({
        name: "Embed API",
        purpose: "embedding",
        models: [],
      }),
    ).toBe(true);
    expect(
      isEmbeddingLaneProvider({
        name: "legacy-embed",
        models: [{ embedding: true }, { task: "embedding" }],
      }),
    ).toBe(true);
    expect(
      isEmbeddingLaneProvider({
        name: "mixed",
        models: [{ embedding: true }, { id: "gpt-4o" }],
      }),
    ).toBe(false);
  });
});

describe("isOllamaProviderRow", () => {
  it("matches onboard identity only — not a coincidental Ollama URL", () => {
    expect(isOllamaProviderRow({ name: "anything", api_key: "ollama" })).toBe(
      true,
    );
    expect(isOllamaProviderRow({ name: "Ollama (Local)" })).toBe(true);
    expect(isOllamaProviderRow({ name: "  ollama (local)  " })).toBe(true);
    expect(
      isOllamaProviderRow({
        name: "home-ollama",
        api_key: "sk-x",
        base_url: "http://127.0.0.1:11434",
      }),
    ).toBe(false);
    expect(
      isOllamaProviderRow({
        name: "docker-ollama",
        api_key: "sk-x",
        base_url: "http://ollama:11434",
      }),
    ).toBe(false);
  });

  it("does not treat custom local OpenAI-compat providers as onboard Ollama", () => {
    expect(
      isOllamaProviderRow({
        name: "my-local-llm",
        api_key: "sk-local",
        base_url: "http://127.0.0.1:8000/v1",
      }),
    ).toBe(false);
    expect(
      isLocalProviderRow({
        name: "lm-studio",
        api_key: "lm-studio",
        base_url: "http://localhost:1234/v1",
      }),
    ).toBe(false);
    expect(
      isLocalProviderRow({
        name: "company-ollama",
        api_key: "sk-x",
        base_url: "http://192.168.1.10:11434",
      }),
    ).toBe(false);
  });
});

describe("looksLikeOllamaProvider", () => {
  it("keeps URL hints for the Ollama management UI", () => {
    expect(isOllamaRuntimeUrl("http://127.0.0.1:11434")).toBe(true);
    expect(isOllamaRuntimeUrl("http://ollama")).toBe(true);
    expect(isOllamaRuntimeUrl("http://host/qwen11434")).toBe(false);
    expect(isOllamaRuntimeUrl("https://ollama.com/v1")).toBe(false);
    expect(
      looksLikeOllamaProvider({
        name: "company-ollama",
        api_key: "sk-x",
        base_url: "http://192.168.1.10:11434",
      }),
    ).toBe(true);
    expect(
      looksLikeOllamaProvider({
        name: "my-local-llm",
        api_key: "sk-local",
        base_url: "http://127.0.0.1:8000/v1",
      }),
    ).toBe(false);
  });
});

describe("groupPresets", () => {
  it("groups iflytek as a brand group even with a single variant", () => {
    const result = groupPresets([
      {
        id: "iflytek-astron-token-plan",
        name: "iFlytek Astron Token Plan",
        provider_group: "iflytek",
        provider_group_name: "iFlytek Astron",
        provider_variant: "token_plan",
        protocol: "openai",
        base_url: "https://maas-token-api.cn-huabei-1.xf-yun.com/v2",
        models: [],
      },
    ]);
    expect(result.grouped).toHaveLength(1);
    expect(result.grouped[0].groupKey).toBe("iflytek");
    expect(result.grouped[0].groupName).toBe("iFlytek Astron");
    expect(result.ungrouped).toHaveLength(0);
  });

  it("pins 雪浪工匠 as a single-site card ahead of brand groups", () => {
    const xuelang = {
      id: "xuelang-gongjiang",
      name: "雪浪工匠",
      protocol: "openai",
      base_url: "https://api.xuelanglm.com/v1",
      models: [],
    };
    const tencentCoding = {
      id: "tencent-coding-plan",
      name: "Tencent Cloud Coding Plan",
      provider_group: "tencent",
      provider_group_name: "Tencent Cloud",
      provider_variant: "coding_plan",
      protocol: "openai",
      base_url: "https://api.lkeap.cloud.tencent.com/coding/v3",
      models: [],
    };
    const deepseek = {
      id: "deepseek",
      name: "DeepSeek",
      protocol: "openai",
      base_url: "https://api.deepseek.com/v1",
      models: [],
    };
    // No provider_group on the preset itself — single site, no variant picker.
    expect("provider_group" in xuelang && xuelang.provider_group).toBeFalsy();

    const { pinned, rest } = splitPinnedPresets([
      tencentCoding,
      xuelang,
      deepseek,
    ]);
    expect(pinned).toEqual([xuelang]);
    expect(rest).toEqual([tencentCoding, deepseek]);

    // Without pinning, groupPresets sorts ungrouped alphabetically —
    // "DeepSeek" lands ahead of "雪浪工匠", hence the dedicated pin mechanism.
    const { grouped, ungrouped } = groupPresets([xuelang, deepseek]);
    expect(grouped).toHaveLength(0);
    expect(ungrouped.map((p) => p.id)).toEqual([
      "deepseek",
      "xuelang-gongjiang",
    ]);
  });
});
