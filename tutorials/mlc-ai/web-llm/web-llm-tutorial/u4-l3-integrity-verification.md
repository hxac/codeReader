# 模型完整性校验：SRI 子资源完整性

## 1. 本讲目标

学完本讲，你应该能够：

1. 写出一个合法的 SRI 字符串，并解释它由哪三部分信息构成（算法、Base64 哈希、解码后字节数）。
2. 读懂 `parseSRI` / `isValidSRI` 的两层校验逻辑（正则匹配 + 字节数推算），并亲手算出 sha256 / sha384 / sha512 对应的 Base64 段长度。
3. 追踪 `verifyIntegrity` 的完整流程：整块读入 `ArrayBuffer` → Web Crypto 计算摘要 → Base64 编码 → 字符串精确比对 → 按策略抛 `IntegrityError` 或打警告。
4. 说清引擎在 `reload()` 中把完整性校验挂在哪三个位置（config、model_lib、tokenizer），以及哪些产物**不在**校验范围内。
5. 独立为自己的自定义模型生成 SRI 哈希并配置到 `ModelRecord.integrity` 上，完成一次「校验通过」与一次「校验失败」的对照实验。

## 2. 前置知识

### 2.1 哈希函数与 SHA-2 家族

密码学哈希函数能把任意长度的字节串映射成固定长度的「指纹」。它的两个关键性质是：

- **抗碰撞性**：想找到两段不同内容却有相同哈希，在计算上不可行。
- **雪崩效应**：改动一个字节，哈希值看起来完全变了。

所以「内容 → 哈希」几乎是单向的身份证明：拿到文件后重算一遍哈希，与预期哈希比对，即可确认文件在传输途中没有被篡改或损坏。WebLLM 支持的是 SHA-2 家族的三个成员：

| 算法 | 摘要长度（字节） | Base64 编码后长度（字符） | 末尾 `=` 个数 |
| --- | --- | --- | --- |
| SHA-256 | 32 | 44 | 1 |
| SHA-384 | 48 | 64 | 0 |
| SHA-512 | 64 | 88 | 2 |

（这张表的推导见 4.1.2 节，先记住结论即可。）

### 2.2 Base64 编码

哈希是**二进制字节串**，但配置文件里只能写**文本**。Base64 就是把 3 个字节（24 位）拆成 4 个「6 位的字符」，映射到 `A-Z a-z 0-9 + /` 这 64 个字符上。末尾不足 3 字节时用 `=` 补齐标志。因此 `=` 只能出现在末尾，且最多 2 个——这个约束正是后面源码校验的重点之一。

### 2.3 SRI（Subresource Integrity，子资源完整性）

SRI 是 Web 平台的一个安全机制：在 HTML 里写

```html
<script src="https://cdn.example.com/app.js"
        integrity="sha256-BASE64..." crossorigin="anonymous"></script>
```

浏览器下载脚本后会先算哈希、与 `integrity` 属性比对，不一致就拒绝执行。它防的是「CDN 被攻破、脚本被偷换」这类供应链攻击。WebLLM 借用了同一个字符串格式（`算法-Base64哈希`）来校验从远端 URL 下载的模型产物，这就是本讲的 `integrity.ts`。

### 2.4 Web Crypto API 与安全上下文

`crypto.subtle.digest("SHA-256", data)` 是浏览器与 Node.js（18+）都内置的标准摘要接口。注意 `crypto.subtle` **只在安全上下文可用**——即 HTTPS 页面或 `localhost`。这也是本地开发时要用 `localhost` 而不是裸 IP 访问示例页面的原因之一。

### 2.5 承接上一讲

u4-l1 讲过 `reload()` 要持久化四类产物，分存三个缓存作用域：`webllm/config`（聊天配置）、`webllm/wasm`（模型库）、`webllm/model`（tokenizer 与权重）。本讲讲的正是「这些产物从网络进入缓存/内存之前，如何先验明正身」。建议先回顾 u4-l2 中 `fetchWithCache`「命中读缓存、未命中走网络并回填」的行为——校验就插在「拿到字节」与「使用字节」之间。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/integrity.ts` | 完整性校验的核心模块（约 150 行） | `ModelIntegrity` 类型、`parseSRI`、`getDecodedBase64ByteLength`、`verifyIntegrity`、`isValidSRI` |
| `src/error.ts` | 全部错误类定义 | `IntegrityError` 的字段与文案 |
| `src/engine.ts` | 引擎编排 | `reloadInternal` 中 config 与 wasm 两个校验挂载点 |
| `src/cache_util.ts` | 缓存工具 | `maybeVerifyTokenizerIntegrity` 与 `asyncLoadTokenizer`（tokenizer 挂载点） |
| `src/config.ts` | 模型配置体系 | `ModelRecord.integrity` 字段、`modelLibURLPrefix` / `modelVersion` |
| `src/index.ts` | 库入口 | 完整性相关 API 的对外导出 |
| `tests/integrity.test.ts` | 单元测试（无需 GPU） | `computeSRI` 帮助函数与各边界用例 |
| `examples/integrity-verification/` | 完整性校验示例 | `src/integrity_verification.ts` 与 `package.json` |

> 说明：`examples/integrity-verification/` 目录下**没有** README.md，只有 `package.json` 和 `src/` 两个源文件。该示例的文档实际写在主 [README.md:L344-L395](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L344-L395) 的 "Integrity Verification" 章节，本讲以真实存在的文件为准。

## 4. 核心概念与源码讲解

### 4.1 SRI 字符串：格式、解析与合法性校验（parseSRI 与 isValidSRI）

#### 4.1.1 概念说明

一条 SRI 字串形如：

```
sha256-MV9b23bQeMQ7isAGTkoBZGErH853yGk0W/yUx1iU7dM=
```

它由连字符分成两段：算法名 `sha256` 与 Base64 编码的哈希值。WebLLM 只接受 sha256 / sha384 / sha512 三种算法——sha1、md5 因已不具备安全强度而被拒之门外。

`ModelIntegrity` 接口描述了「一个模型的哪些产物要校验、哈希各是多少」：

```ts
export interface ModelIntegrity {
  config?: SRIString;            // mlc-chat-config.json 的哈希
  model_lib?: SRIString;         // wasm 模型库的哈希
  tokenizer?: FileIntegrityMap;  // 按文件名索引的 tokenizer 哈希
  onFailure?: "error" | "warn";  // 失败时抛错还是警告
}
```

见 [src/integrity.ts:L26-L31](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/integrity.ts#L26-L31)。四个字段全部可选——**只校验写明了哈希的产物**，整个 `integrity` 字段不写则完全不校验、行为与从前一致（README 明确承诺了这一点）。

#### 4.1.2 核心流程

`parseSRI(sri)` 做两层校验，任何一层不过都返回 `null`：

```text
输入字符串
  │
  ├─ 第一层：正则 /^(sha256|sha384|sha512)-([A-Za-z0-9+/]+={0,2})$/
  │    · 算法名必须三选一
  │    · 哈希段只能含 Base64 字符，= 只能在末尾且最多 2 个
  │
  ├─ 第二层：getDecodedBase64ByteLength(hash) 推算解码后字节数
  │    · 长度 mod 4 == 1            → 非法（Base64 不存在这种长度）
  │    · padding > 2                → 非法
  │    · 有 padding 但长度不是 4 的倍数 → 非法
  │    · 推算字节数 ≠ 该算法摘要长度   → 非法
  │
  └─ 两层都过 → 返回 { algo, hash }
```

第二层的字节数推算用到的数学很简单。设 Base64 段长度为 \( n \)，填充字符数为 \( p \)，则解码后的字节数为：

\[
\text{decodedLength} \;=\; \left\lfloor \frac{n}{4} \right\rfloor \times 3 \;-\; p \;+\; r, \qquad r = \begin{cases} 0 & n \bmod 4 = 0 \\ 1 & n \bmod 4 = 2 \\ 2 & n \bmod 4 = 3 \end{cases}
\]

（\( n \bmod 4 = 1 \) 直接判非法。）用 sha256 验证一下：32 字节 → 10 个完整四元组（30 字节、40 字符）+ 剩 2 字节（3 字符 + 1 个 `=`）→ \( n = 44,\ p = 1 \)，\( 11 \times 3 - 1 = 32 \) ✓。这正是 2.1 节那张表的来源。

#### 4.1.3 源码精读

先看常量定义。[src/integrity.ts:L35-L47](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/integrity.ts#L35-L47) 定义了正则、每种算法的摘要字节数、以及算法名到 Web Crypto 名称的映射（`sha256` → `"SHA-256"`）：

```ts
const SRI_REGEX = /^(sha256|sha384|sha512)-([A-Za-z0-9+/]+={0,2})$/;

const SRI_HASH_BYTE_LENGTH: Record<SRIAlgorithm, number> = {
  sha256: 32, sha384: 48, sha512: 64,
};
```

接着是字节数推算函数 [src/integrity.ts:L49-L77](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/integrity.ts#L49-L77)，逐步落实 4.1.2 的规则，关键三行：

```ts
const fullQuartets = Math.floor(length / 4);
let decodedLength = fullQuartets * 3;
if (padding > 0) { decodedLength -= padding; }
else if (remainder === 2) { decodedLength += 1; }
else if (remainder === 3) { decodedLength += 2; }
```

最后是解析主函数与对外校验函数。[src/integrity.ts:L79-L93](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/integrity.ts#L79-L93) 中 `parseSRI` 先过正则，再比对字节数， mismatch 就返回 `null`：

```ts
function parseSRI(sri: string): { algo: SRIAlgorithm; hash: string } | null {
  const match = sri.match(SRI_REGEX);
  if (!match) { return null; }
  const algo = match[1] as SRIAlgorithm;
  const hash = match[2];
  const decodedLength = getDecodedBase64ByteLength(hash);
  if (decodedLength !== SRI_HASH_BYTE_LENGTH[algo]) { return null; }
  return { algo, hash };
}
```

[src/integrity.ts:L148-L150](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/integrity.ts#L148-L150) 的 `isValidSRI` 只是 `parseSRI` 的布尔包装，它被导出到 npm 包入口（见 [src/index.ts:L15-L21](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/index.ts#L15-L21)），方便应用作者在把用户提供的哈希写进配置前先做格式检查。

测试文件里有一组现成的边界用例可以对照：拒绝非法算法（`sha1-...`、`md5-...`，[tests/integrity.test.ts:L51-L54](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/integrity.test.ts#L51-L54)）、拒绝超量填充（`sha256-abc===`，[tests/integrity.test.ts:L69-L73](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/integrity.test.ts#L69-L73)）、拒绝「sha256 前缀配 64 字节哈希」的长度错配（[tests/integrity.test.ts:L75-L78](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/integrity.test.ts#L75-L78)）。

#### 4.1.4 代码实践

1. **实践目标**：通过真实测试用例验证自己对 SRI 格式规则的理解。
2. **操作步骤**：在仓库根目录运行：

   ```bash
   npx jest tests/integrity.test.ts -t "isValidSRI"
   ```

   （根目录需已执行过 `npm install`；`npm test` 会跑全部测试并带覆盖率，这里用 `-t` 只筛选 `isValidSRI` 描述块。）
3. **需要观察的现象**：终端输出 `isValidSRI` 组下约 10 个用例全部通过。
4. **预期结果**：所有用例绿色通过。随后自己动笔回答：为什么 `sha256-A=`（长度 2、含 1 个 `=`，即「有 padding 但长度不是 4 的倍数」）会被 [tests/integrity.test.ts:L81-L82](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/integrity.test.ts#L81-L82) 判为非法？（答案见下面练习第 3 题。）本组测试不依赖 GPU 与网络，任何环境都可运行；具体输出以本地为准。

#### 4.1.5 小练习与答案

**练习 1**：不带 `-` 分隔符的裸 Base64 哈希（如 `MV9b23bQ...`）能通过 `isValidSRI` 吗？

> **答案**：不能。`SRI_REGEX` 要求必须匹配 `算法-哈希` 两段结构，缺少 `sha256-` 等前缀时 `match` 返回 `null`（对应测试 [tests/integrity.test.ts:L56-L58](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/integrity.test.ts#L56-L58) 的 "rejects missing algorithm prefix"）。

**练习 2**：一个合法 sha384 SRI 的 Base64 段应有多少个字符、几个 `=`？

> **答案**：48 字节恰是 3 的整数倍（48 = 3×16），编码成 16 个完整四元组共 64 字符、**无** `=` 填充。可以用公式 \( \lfloor 64/4 \rfloor \times 3 = 48 \) 反向验证。

**练习 3**：为什么 `sha256-A=` 非法？

> **答案**：长度 2 mod 4 = 2 ≠ 0，却出现了填充字符 `=`。Base64 规则规定 `=` 只用于把最后一组补齐成 4 字符，所以「有 padding 但总长不是 4 的倍数」自相矛盾，[src/integrity.ts:L61-L63](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/integrity.ts#L61-L63) 直接返回 `null`。

### 4.2 verifyIntegrity：整块哈希校验与 onFailure 策略

#### 4.2.1 概念说明

`verifyIntegrity` 是唯一真正执行校验的函数，签名：

```ts
export async function verifyIntegrity(
  data: ArrayBuffer,       // 待校验的原始字节（已完整读入内存）
  expectedSRI: SRIString,  // 期望的 SRI 哈希
  url: string,             // 产物 URL，仅用于报错信息
  onFailure: "error" | "warn" = "error",
): Promise<void>
```

需要澄清一个容易误解的点：**当前实现并不是流式（streaming）校验**。调用方（引擎）先用 `fetchWithCache(url, "arraybuffer")` 把整个文件读成一块 `ArrayBuffer`，`verifyIntegrity` 再对这块内存做一次性的 `crypto.subtle.digest`。它的安全价值不在于「边下边算」，而在于**时序**——校验发生在字节被「使用」之前：config 在 `JSON.parse` 之前、wasm 在 `tvmjs.instantiate` 之前、tokenizer 在 `Tokenizer.fromJSON` 之前。哈希不匹配的字节永远走不到解析/实例化那一步。

`onFailure` 是失败策略开关：`"error"`（默认）抛 `IntegrityError` 阻断加载；`"warn"` 仅用 loglevel 打一条警告并放行，适合「想在开发期观察、但不想因为远端文件小改动就打断用户」的场景。

#### 4.2.2 核心流程

```text
verifyIntegrity(data, expectedSRI, url, onFailure)
  │
  ├─ parseSRI(expectedSRI)
  │     └─ null → throw new Error("Invalid SRI hash format: ...")   ← 注意：普通 Error，不是 IntegrityError
  │
  ├─ hashBuffer = await crypto.subtle.digest("SHA-256" 等算法名, data)
  │
  ├─ 逐字节转字符串 → btoa() → actualHash（Base64）
  │
  └─ actualHash !== expectedHash ?
        ├─ 是，且 onFailure === "warn"  → log.warn(...) 后正常返回
        └─ 是，且 onFailure === "error"  → throw new IntegrityError(url, expectedSRI, actualSRI)
```

一个值得注意的细节：**格式错误与内容错误走两条不同的异常路径**。SRI 字符串本身写错（比如算法拼错、哈希长度不对），抛的是带 `Invalid SRI hash format` 文案的普通 `Error`——这是配置错误，锅在写配置的人；而格式合法但与实际内容不符，抛的才是 `IntegrityError`——这才意味着「文件可能被动过」。

#### 4.2.3 源码精读

完整实现位于 [src/integrity.ts:L104-L140](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/integrity.ts#L104-L140)。分三段看：

**第一段：格式把关**（[src/integrity.ts:L110-L118](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/integrity.ts#L110-L118)）——先 `parseSRI`，失败即抛普通 `Error`，并把期望格式写进报错文案方便排查：

```ts
const parsed = parseSRI(expectedSRI);
if (!parsed) {
  throw new Error(
    `Invalid SRI hash format: "${expectedSRI}". ` +
      `Expected format: "sha256-BASE64", "sha384-BASE64", or "sha512-BASE64".`,
  );
}
```

**第二段：算哈希并转 Base64**（[src/integrity.ts:L119-L127](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/integrity.ts#L119-L127)）——`ALGO_MAP` 把 `sha256` 译成 Web Crypto 需要的 `"SHA-256"`；摘要字节再经 `String.fromCharCode` + `btoa` 变成 Base64 文本：

```ts
const hashBuffer = await crypto.subtle.digest(ALGO_MAP[algo], data);
const hashArray = new Uint8Array(hashBuffer);
let binary = "";
for (let i = 0; i < hashArray.length; i++) {
  binary += String.fromCharCode(hashArray[i]);
}
const actualHash = btoa(binary);
```

**第三段：比对与分流**（[src/integrity.ts:L129-L139](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/integrity.ts#L129-L139)）——字符串精确相等才放行；不等时按 `onFailure` 分流，`warn` 分支的日志同时包含 URL、期望哈希与实际哈希：

```ts
if (actualHash !== expectedHash) {
  const actualSRI = `${algo}-${actualHash}`;
  if (onFailure === "warn") {
    log.warn(`Integrity check failed for ${url}. Expected: ${expectedSRI}, Got: ${actualSRI}`);
    return;
  }
  throw new IntegrityError(url, expectedSRI, actualSRI);
}
```

`IntegrityError` 本体在 [src/error.ts:L615-L629](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L615-L629)。它把 `url`、`expected`、`actual` 三个字段声明为 `readonly` 构造参数，调用方（如示例页面）可以直接取出来展示：

```ts
export class IntegrityError extends Error {
  constructor(
    readonly url: string,
    readonly expected: string,
    readonly actual: string,
  ) {
    super(
      `Integrity verification failed for ${url}\n` +
        `  Expected: ${expected}\n` +
        `  Actual: ${actual}\n` +
        `This may indicate file corruption or tampering.`,
    );
    this.name = "IntegrityError";
  }
}
```

文案最后一句 "This may indicate file corruption or tampering" 直接点明了这个错误的语义：损坏或篡改。

测试侧有两个「不是自说自话」的用例值得一看：用公开已知的空串 SHA-256 与 `"abc"` 的 SHA-256 做基准（[tests/integrity.test.ts:L113-L130](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/integrity.test.ts#L113-L130)），保证实现产出的是**正确**哈希而不只是「自己算的和自己比」；以及 1MB 大数据的用例（[tests/integrity.test.ts:L265-L282](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/integrity.test.ts#L265-L282)），验证整块摘要没有大小限制问题。测试文件顶部的 `computeSRI` 帮助函数（[tests/integrity.test.ts:L6-L21](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/integrity.test.ts#L6-L21)）就是 `verifyIntegrity` 第二段逻辑的反向复制品——测试用同一套编码方式生成期望值。

#### 4.2.4 代码实践

1. **实践目标**：亲手制造一次「哈希不匹配」，观察 `IntegrityError` 的三个字段。
2. **操作步骤**：在 `tests/` 下新建临时文件 `integrity_handson.test.ts`（练习文件，不提交）：

   ```ts
   import { verifyIntegrity } from "../src/integrity";
   import { IntegrityError } from "../src/error";
   import { describe, test, expect } from "@jest/globals";

   describe("hands-on: tamper detection", () => {
     test("modified data triggers IntegrityError with three fields", async () => {
       const data = new TextEncoder().encode("original bytes").buffer;
       // 故意写一个格式合法但必然错误的哈希（43 字符 + 1 个 =）
       const fakeSRI = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
       try {
         await verifyIntegrity(data, fakeSRI, "https://example.com/x.bin");
         throw new Error("should not reach here");
       } catch (err) {
         expect(err).toBeInstanceOf(IntegrityError);
         const e = err as IntegrityError;
         console.log("url:      ", e.url);
         console.log("expected: ", e.expected);
         console.log("actual:   ", e.actual);
       }
     });
   });
   ```

   运行 `npx jest tests/integrity_handson.test.ts`。
3. **需要观察的现象**：控制台打印的三行里，`actual` 是 `sha256-` 开头、44 字符的哈希——正是 `original bytes` 这段文本真实的 SHA-256。
4. **预期结果**：用例通过，且 `expected` 与 `actual` 明显不同。可进一步把 `onFailure` 参数改为 `"warn"` 再跑一次，观察不抛错、只在日志里出现 `Integrity check failed`（参照 [tests/integrity.test.ts:L187-L209](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/integrity.test.ts#L187-L209) 的做法）。实际打印内容以本地运行为准。

#### 4.2.5 小练习与答案

**练习 1**：`verifyIntegrity(buf, "sha256-short", url)` 会抛 `IntegrityError` 吗？

> **答案**：不会。`sha256-short` 过不了 `parseSRI`（Base64 段解码后不足 32 字节），抛的是文案为 `Invalid SRI hash format` 的普通 `Error`（[src/integrity.ts:L111-L116](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/integrity.ts#L111-L116)）。`IntegrityError` 只在「格式合法但内容对不上」时出现（[tests/integrity.test.ts:L234-L238](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/integrity.test.ts#L234-L238) 验证了这一点）。

**练习 2**：为什么 `verifyIntegrity` 里要用 `btoa(binary)` 而不是直接比较 `hashBuffer` 与期望值？

> **答案**：期望值是 SRI **文本**（Base64），而刚算出的摘要是二进制 `ArrayBuffer`，两者不同型。把摘要编码成 Base64 后即可做字符串严格相等比较，同时 `actualSRI` 还能原样塞进 `IntegrityError` 展示给用户。

**练习 3**：哈希比对为什么用 `!==` 整串比较，而不是逐字符模糊匹配？把 `actual` 与 `expected` 的前几位改相同会怎样？

> **答案**：密码学哈希的输出每一位都均匀且敏感，整串精确相等是唯一正确的判据；哪怕只差一个字符也意味着内容不同。测试 [tests/integrity.test.ts:L284-L300](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/integrity.test.ts#L284-L300) 用「只改动 5 个字节中的 1 个」验证了这一点——照样抛 `IntegrityError`。

### 4.3 IntegrityError 与引擎集成：三个校验挂载点

#### 4.3.1 概念说明

校验函数只有被挂到正确的时机才能发挥作用。WebLLM 把 `ModelRecord.integrity`（字段定义见 [src/config.ts:L275-L286](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L275-L286)，`integrity?: ModelIntegrity` 在 [L285](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L285)）接进了 `reload()` 主流程的三个位置，分别对应三类产物：

| 挂载点 | 校验的文件 | 校验时机（「使用」之前的一步） | 所在文件 |
| --- | --- | --- | --- |
| ① config | `mlc-chat-config.json` | `fetchWithCache` 之后、`JSON.parse` 之前 | `src/engine.ts` |
| ② model_lib | wasm 模型库 | `fetchWasmSource` 之后、`tvmjs.instantiate` 之前 | `src/engine.ts` |
| ③ tokenizer | `tokenizer.json` / `tokenizer.model` | `fetchWithCache` 之后、`Tokenizer.fromJSON/fromSentencePiece` 之前 | `src/cache_util.ts` |

同时要诚实地指出边界：**模型权重分片（`.bin` 张量文件）不在校验范围内**——它们由 `tvm.fetchTensorCache` 直接拉取（见 [src/engine.ts:L394-L397](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L394-L397)），`ModelIntegrity` 也没有为权重提供字段。此外 u4-l2 提过缓存语义：校验发生在每次取得字节之后，所以缓存命中时同样会被校验——被篡改的缓存不会绕过检查。

#### 4.3.2 核心流程

`reloadInternal` 中与完整性相关的时序（承接 u4-l1 的 reload 全流程）：

```text
reloadInternal(modelId)
  │
  ├─ findModelRecord(modelId)                      ← 取出 ModelRecord（含 integrity）
  ├─ modelUrl = cleanModelUrl(modelRecord.model)
  │
  ├─ ① configCache.fetchWithCache(configUrl)       ← 拿到 mlc-chat-config.json 字节
  │     └─ integrity?.config 存在？
  │           └─ 是 → verifyIntegrity(configData, ..., onFailure)
  │                ├─ 失败+error → 抛 IntegrityError，reload 终止
  │                └─ 失败+warn  → 打警告继续
  │     └─ JSON.parse + overrides + chatOpts 三层合并
  │
  ├─ ② fetchWasmSource()                           ← 拿到 wasm 字节（localhost 不走缓存）
  │     └─ integrity?.model_lib 存在？
  │           └─ 是 → verifyIntegrity(wasmSource, ..., onFailure)
  │     └─ tvmjs.instantiate(wasm.buffer, ...)      ← 校验通过才实例化
  │
  ├─ detectGPUDevice / initWebGPU
  │
  ├─ ③ asyncLoadTokenizer(modelUrl, ..., modelRecord.integrity)
  │     └─ 内部对实际加载的那个 tokenizer 文件调用 maybeVerifyTokenizerIntegrity
  │
  └─ tvm.fetchTensorCache(...)                     ← 权重：无完整性校验
```

三个挂载点全部位于权重装载与管线构造**之前**，因此任何一处 `IntegrityError` 都会让 `reload()` 整体失败，不会留下「半加载」状态。

#### 4.3.3 源码精读

**挂载点①：config 校验**。[src/engine.ts:L278-L296](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L278-L296) 中，配置字节从 `webllm/config` 作用域缓存取得后立刻校验，通过后才 `JSON.parse` 并做三层合并：

```ts
const configUrl = new URL("mlc-chat-config.json", modelUrl).href;
const configData = (await configCache.fetchWithCache(configUrl, "arraybuffer", ...)) as ArrayBuffer;
if (modelRecord.integrity?.config) {
  await verifyIntegrity(
    configData,
    modelRecord.integrity.config,
    configUrl,
    modelRecord.integrity.onFailure,
  );
}
```

注意 `integrity?.config` 的可选链写法——没配哈希就整段跳过，零开销。

**挂载点②：wasm 校验**。[src/engine.ts:L309-L334](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L309-L334) 中，`fetchWasmSource` 先按 URL 类型分流（`localhost` 不缓存、同服务器相对路径走普通 fetch、其余走 `webllm/wasm` 缓存——见 [src/engine.ts:L309-L324](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L309-L324)），无论哪条路，拿到字节后统一校验：

```ts
const wasmSource = await fetchWasmSource();
if (modelRecord.integrity?.model_lib) {
  await verifyIntegrity(
    wasmSource,
    modelRecord.integrity.model_lib,
    wasmUrl,
    modelRecord.integrity.onFailure,
  );
}
const wasm = new Uint8Array(wasmSource);
const tvm = await tvmjs.instantiate(wasm.buffer, ...);   // 校验通过才实例化
```

**挂载点③：tokenizer 校验**。引擎把整个 `modelRecord.integrity` 传给 [asyncLoadTokenizer 调用](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L387-L393)（[src/engine.ts:L387-L393](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L387-L393)）。真正的分发在 [src/cache_util.ts:L47-L57](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L47-L57) 的 `maybeVerifyTokenizerIntegrity`——它按**实际加载的文件名**去 `integrity.tokenizer` 这个 map 里查哈希，查到才校验：

```ts
async function maybeVerifyTokenizerIntegrity(
  data: ArrayBuffer, filename: string, url: string, integrity?: ModelIntegrity,
): Promise<void> {
  const hash = integrity?.tokenizer?.[filename];
  if (hash) {
    await verifyIntegrity(data, hash, url, integrity?.onFailure);
  }
}
```

它被 `asyncLoadTokenizer` 的两个分支各自调用一次：`tokenizer.json` 优先分支在 [src/cache_util.ts:L165-L174](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L165-L174)（校验后 `Tokenizer.fromJSON`），`tokenizer.model` 回退分支在 [src/cache_util.ts:L183-L191](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L183-L191)（校验后 `Tokenizer.fromSentencePiece`）。这也解释了 `tokenizer` 字段为什么设计成 `Record<文件名, SRIString>` 而不是单条哈希：模型仓库里两种 tokenizer 文件可能并存，实际用哪个取决于 `mlc-chat-config.json` 的 `tokenizer_files` 声明，按文件名索引可以对两者分别钉住哈希。

**示例工程**：[examples/integrity-verification/src/integrity_verification.ts:L29-L55](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/integrity-verification/src/integrity_verification.ts#L29-L55) 演示了带 `integrity` 字段的 `appConfig` 写法（L44-L52 是被注释掉的 `integrity` 模板，等待读者填入真实哈希）；页面在 [L75-L86](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/integrity-verification/src/integrity_verification.ts#L75-L86) 用 `instanceof webllm.IntegrityError` 捕获并把 `url / expected / actual` 三字段显示出来。示例启动脚本是 `npm start`（Parcel，端口 8888），见 [examples/integrity-verification/package.json:L6-L7](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/integrity-verification/package.json#L6-L7)。

#### 4.3.4 代码实践

1. **实践目标**：用仓库 README 记载的标准命令为一个真实文件生成 SRI，并当场验证其格式合法性。
2. **操作步骤**：
   1. 在终端对任意本地文件（比如示例的 HTML）执行 README "Integrity Verification" 章节给出的命令（[README.md:L378-L387](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L378-L387)）：

      ```bash
      openssl dgst -sha256 -binary examples/integrity-verification/src/integrity_verification.html \
        | openssl base64 -A | sed 's/^/sha256-/'
      ```

   2. 记下输出（应形如 `sha256-xxxx...=`，44 字符哈希段）。
   3. 把输出粘到下面这个最小页面（示例代码）里验证：

      ```ts
      // 示例代码：浏览器 Console 或任意页面中执行
      import * as webllm from "@mlc-ai/web-llm";
      console.log(webllm.isValidSRI("sha256-把openssl输出粘到这里"));
      ```
3. **需要观察的现象**：`openssl` 输出以 `sha256-` 开头、以一个 `=` 结尾；`isValidSRI` 返回 `true`。
4. **预期结果**：返回 `true`。若把末尾 `=` 删掉再试，应返回 `false`（长度 43 解码出 32.25 字节不成立）。openssl 命令在 macOS/Linux 原生可用，Windows 需按 README 建议走 Git Bash 或 WSL。命令实际输出以本地为准。

#### 4.3.5 小练习与答案

**练习 1**：如果只配置了 `integrity.config` 而没配 `model_lib`，wasm 还会被校验吗？

> **答案**：不会。三个挂载点相互独立，各自检查 `integrity?.xxx` 是否存在（wasm 处的判断在 [src/engine.ts:L327](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L327)）。README 也明确说明 "All fields in `integrity` are optional — only specified artifacts will be verified"（[README.md:L391-L393](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L391-L393)）。

**练习 2**：`onFailure: "warn"` 时模型加载会被阻断吗？这个模式适合什么场景？

> **答案**：不会阻断，只记一条含 URL 与两个哈希的警告后照常加载（[src/integrity.ts:L131-L137](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/integrity.ts#L131-L137)）。适合灰度观察：上游重新发布了同一 URL 的文件（哈希自然变化）时，不希望所有老版本应用立刻不可用，但又想在日志里留下痕迹。

**练习 3**：为什么 tokenizer 的哈希要按文件名做 map，而 config / wasm 用单条哈希就够了？

> **答案**：config 与 wasm 的 URL 由 `ModelRecord` 唯一确定，一对一；而 tokenizer 存在 `tokenizer.json` / `tokenizer.model` 两个候选，实际加载哪个要到读了 `mlc-chat-config.json` 的 `tokenizer_files` 才知道（[src/cache_util.ts:L165-L191](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L165-L191) 的两个分支），所以用 `FileIntegrityMap` 按文件名索引，`maybeVerifyTokenizerIntegrity` 在拿到文件后按名查表。

## 5. 综合实践

综合实践把三个模块串起来：**为一个真实 wasm 生成并钉住 SRI 哈希，再故意「篡改」验证防御生效**。全程使用仓库真实存在的示例工程与 README 记载的命令。

### 第一步：准备示例工程

```bash
cd examples/integrity-verification
npm install
```

示例默认的 `model_lib` 指向 `webllm.modelLibURLPrefix + webllm.modelVersion + "/Llama-3.2-1B-Instruct-q4f16_1-ctx4k_cs1k-webgpu.wasm"`（[examples/integrity-verification/src/integrity_verification.ts:L35-L38](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/integrity-verification/src/integrity_verification.ts#L35-L38)）。其中前缀与版本常量定义在 [src/config.ts:L333-L335](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L333-L335)：

```ts
export const modelVersion = "v0_2_84/base";
export const modelLibURLPrefix =
  "https://raw.githubusercontent.com/mlc-ai/binary-mlc-llm-libs/main/web-llm-models/";
```

> 小提示：示例里的 `ctx4k_cs1k` 文件名与 `prebuiltAppConfig` 当前实际登记的 `Llama-3.2-1B-Instruct-q4f16_1_cs1k-webgpu.wasm`（见 [src/config.ts:L375-L377](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L375-L377)）略有出入。为保证 URL 真实存在，下面统一用 `prebuiltAppConfig` 里那个文件名。

### 第二步：下载 wasm 并生成 SRI

```bash
curl -LO "https://raw.githubusercontent.com/mlc-ai/binary-mlc-llm-libs/main/web-llm-models/v0_2_84/base/Llama-3.2-1B-Instruct-q4f16_1_cs1k-webgpu.wasm"

openssl dgst -sha256 -binary Llama-3.2-1B-Instruct-q4f16_1_cs1k-webgpu.wasm \
  | openssl base64 -A | sed 's/^/sha256-/'
```

记下输出的完整 SRI 字符串。顺手用 `openssl dgst -sha256 <文件>`（hex 版输出）交叉确认：把 hex 形式的摘要按 16 进制转 Base64，应得到同一段哈希——这正是 4.1 节「算法-Base64-字节数」三要素的一次手工演练。

### 第三步：配置并验证通过

编辑 `examples/integrity-verification/src/integrity_verification.ts`（这是你自己的练习副本，可以随意改）：

1. 把 `model_lib` 改为上面的完整 URL；
2. 打开 L44-L52 被注释的 `integrity` 块，把 `model_lib` 字段填成第二步的 SRI 字符串（`config` 与 `tokenizer` 可暂不填——按 4.3 节结论，只校验写明的产物）；
3. `npm start` 后用支持 WebGPU 的浏览器打开 `http://localhost:8888`。

**预期**：模型正常加载并回复 "Hello! What can you do?"——校验通过时用户完全无感，这正是设计目标。

### 第四步：篡改哈希，验证防御生效

把 `integrity.model_lib` 的哈希**任意改动一个字符**（注意：改成另一个 Base64 合法字符，如把某位 `A` 改成 `B`，保持格式合法），刷新页面重新加载。

**预期**：加载中止，页面 status 区域显示 `Integrity verification failed!` 及 URL、Expected、Actual 三行（来自示例的 [L76-L82](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/integrity-verification/src/integrity_verification.ts#L76-L82) 错误处理分支），DevTools Console 中能看到 `IntegrityError` 的完整报错。对照观察两点：

- 如果你把字符改成了**非法字符**（如 `!`），得到的是另一条错误 `Invalid SRI hash format`——4.2 节讲的两条异常路径在此现形。
- 把 `onFailure` 改成 `"warn"` 再试：模型会带着一条 `Integrity check failed` 警告照常加载。

> 第四步在浏览器中的实际表现待本地验证（取决于浏览器与网络环境），但抛错行为本身由 [src/integrity.ts:L129-L139](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/integrity.ts#L129-L139) 与 [tests/integrity.test.ts:L132-L162](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/integrity.test.ts#L132-L162) 的单测双重保证。

### 第五步：回答「防的是什么攻击」

写一段 200 字左右的笔记回答：这个机制防御的攻击面是什么、不防御什么。参考要点：

- **防**：产物在**分发链路**上被替换或损坏——CDN/GitHub raw 内容被篡改、DNS 劫持、镜像站偷换文件、传输截断。wasm 是最敏感的产物：它携带可执行逻辑，一旦被换，攻击者可以操纵模型行为。config 与 tokenizer 被换则可能注入恶意对话模板或间接提示词注入。
- **不防**：哈希本身由应用作者写进自己的 `appConfig`，所以它保证「文件与作者当年审定的那一份逐字节一致」，**不评价源头是否可信**；权重分片不在校验范围（4.3.1 节）；作者自己引用了恶意文件，SRI 无能为力。

## 6. 本讲小结

- SRI 字符串 = `算法-Base64哈希`，`parseSRI` 做两层校验：正则匹配（算法三选一、Base64 字符集、`≤2` 个尾部 `=`）+ 解码字节数精确等于 32/48/64；`isValidSRI` 是其布尔包装并被导出到 npm 入口。
- `verifyIntegrity` 是**整块校验**而非流式：对完整 `ArrayBuffer` 一次 `crypto.subtle.digest`，Base64 编码后整串比对；格式错抛普通 `Error`，内容不符按 `onFailure` 抛 `IntegrityError` 或仅警告。
- `IntegrityError` 携带 `url / expected / actual` 三个只读字段，文案明确指向「损坏或篡改」。
- 引擎在 `reload()` 中有三个独立挂载点：config（`JSON.parse` 前）、model_lib（`instantiate` 前）、tokenizer（按实际加载的文件名查 `FileIntegrityMap`，`fromJSON` 前）；权重分片不校验。
- 安全定位：SRI 钉住的是「分发链路上的逐字节一致性」，防 CDN/镜像/DNS 层面的供应链替换与传输损坏，不替代对源头本身的信任判断。
- 所有完整性单测不依赖 GPU，`npx jest tests/integrity.test.ts` 可在任何环境复跑。

## 7. 下一步学习建议

本讲是单元四（模型分发）的第三讲。下一讲 **u4-l4 接入自定义模型：从 MLC 编译到 appConfig** 将把本讲的 `integrity` 字段放进更大的图景：当你用 `mlc-llm` 编译出自己的 wasm 与权重、自建 `model_list` 时，为自己的产物钉上 SRI 哈希是发布前的最后一步（README 的 "Custom Models" 章节紧挨着 "Integrity Verification"）。建议提前阅读：

- [README.md:L397](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L397) 起的 "Custom Models" 章节，看自定义模型与完整性校验如何在同一个 `ModelRecord` 上汇合。
- [src/config.ts:L333-L335](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L333-L335) 的 `modelLibURLPrefix` / `modelVersion`，思考：官方模型为什么**没有**预置 `integrity` 字段？（提示：模型库随版本频繁更新，哈希维护成本；这留给下游应用按需开启。）
- 若你对「安全」主线感兴趣，可对比 Web 平台原生的 SRI 规范（MDN "Subresource Integrity"），体会 WebLLM 复用其字符串格式但自建校验时机（缓存之后、使用之前）的设计取舍。
