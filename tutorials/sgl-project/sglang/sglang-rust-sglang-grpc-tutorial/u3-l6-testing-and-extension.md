# 测试组织与扩展实践：新增 RPC、新增分词器后端

## 1. 本讲目标

本讲是进阶/专家层的收尾篇。前面十几讲已经把 sglang-grpc 的启动、proto 契约、服务实现、Python 桥接、背压、中止传播、错误映射、Tokio/GIL 协作全部拆解过。本讲换一个视角——**不再读"它怎么跑"，而是读"它怎么被验证、怎么被扩展"**。

学完后你应该能：

- 读懂本 crate 的单元测试组织：四种测试落点、`#[cfg(test)] mod tests;` 的"文件分离"与"内联"两种写法。
- 掌握**串行 env 测试**模式与 `SAFETY` 注释约定，理解为什么改进程级环境变量的测试必须串行、为什么在 edition 2024 下要用 `unsafe`。
- 能照着现有测试的风格，为纯函数（错误消息、JSON 编码、请求字典构建）补写单元测试。
- 理解分词器后端的扩展点 `TokenizerBackend` trait 与 `load_backend` 探测链。
- 拿到**新增一个 gRPC RPC** 与**新增一个分词器后端**两份完整改动清单。

## 2. 前置知识

- **条件编译 `#[cfg(test)]`**：Rust 在 `cargo test` 时会开启 `test` 编译开关，带 `#[cfg(test)]` 的代码只在测试构建里存在，正常 release 产物里完全没有，零运行时开销。
- **`mod tests;` 的模块解析**：一个模块文件 `foo.rs` 内部声明 `mod tests;`，编译器会在 `foo/tests.rs`（或 `foo/tests/mod.rs`）寻找其内容。这就是为什么本 crate 同时存在 `src/server.rs` 与 `src/server/tests.rs`。
- **进程级环境变量是全局可变状态**：`std::env::set_var` 改的是整个进程的环境块，所有线程可见。在 edition 2024 / 新版工具链里，这类操作被标记为 `unsafe`，因为它会引发跨线程数据竞争。
- **trait object（特征对象）**：`Box<dyn Trait>` 把"一组实现了同一 trait 的不同具体类型"统一成一个类型，是 Rust 里做"可插拔后端"的常见手段。
- 本讲承接 [u2-l2 服务实现总览](u2-l2-service-impl-overview.md)（三类 RPC 与公共提交模式）与 [u2-l8 原生分词器与 Python 回退](u2-l8-native-tokenizer.md)（`TokenizerBackend` 与回退策略），建议先读完这两篇。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/lib.rs](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs) | crate 模块声明，决定测试模块挂在哪棵树上 |
| [src/server/tests.rs](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server/tests.rs) | server 的文件分离测试：错误码映射 + env 串行测试 |
| [src/server.rs](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs) | 被测函数本体（`resolve_max_message_size`、`terminal_error_status`、`openai_status_code`、各 RPC） |
| [src/bridge/tests.rs](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge/tests.rs) | bridge 的文件分离测试：`TerminalError::message` 含 rid |
| [src/bridge.rs](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs) | `TerminalError` 定义与 `message()` |
| [src/utils/py_utils.rs](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/py_utils.rs) | 声明 `mod tests;` 与 `json_encode_string` |
| [src/utils/py_utils/tests.rs](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/py_utils/tests.rs) | py_utils 测试：JSON 编码往返 |
| [src/utils/request_utils.rs](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs) | 请求字典构建函数 + 内联测试模块（本讲综合实践的对象） |
| [src/tokenizers.rs](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/tokenizers.rs) | `TokenizerBackend` trait、`load_backend` 探测链、`from_tokenizer_path` 回退关卡 |
| [proto/sglang/runtime/v1/sglang.proto](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto) | 服务契约，新增 RPC 的起点（注意：该文件在 crate 目录之外） |

## 4. 核心概念与源码讲解

### 4.1 测试模块的组织方式：`#[cfg(test)] mod tests` 的两种写法

#### 4.1.1 概念说明

Rust 社区惯例是把单元测试写在被测代码旁边，用 `#[cfg(test)] mod tests { ... }` 包起来。这样测试与实现同模块，能直接 `use super::*;` 访问私有项，又不会进 release 产物。当测试变多、想让源文件保持清爽时，可以把整个 `tests` 模块拆到独立文件，写法变成 `#[cfg(test)] mod tests;`（注意末尾分号）——编译器会去同名子目录里找文件。

sglang-grpc **同时用了两种写法**，这是本节要建立的第一个认知。

#### 4.1.2 核心流程

测试模块的解析路径：

1. `lib.rs` 声明 `pub mod server;` → 解析到 `src/server.rs`。
2. `server.rs` 末尾写 `#[cfg(test)] mod tests;` → 解析到 `src/server/tests.rs`。
3. `utils/mod.rs` 声明 `mod request_utils;` → 解析到 `src/utils/request_utils.rs`；该文件内写**内联** `#[cfg(test)] mod tests { ... }`，测试体就在本文件里。

四种测试落点：

| 测试位置 | 写法 | 被测重点 |
| --- | --- | --- |
| `src/server/tests.rs` | 文件分离 | 错误码映射、env 串行 |
| `src/bridge/tests.rs` | 文件分离 | `TerminalError` 消息 |
| `src/utils/py_utils/tests.rs` | 文件分离 | JSON 编码往返 |
| `src/utils/request_utils.rs` 内 `mod tests` | 内联 | 请求字典字段 |

#### 4.1.3 源码精读

crate 的模块树入口在 `src/lib.rs`：[src/lib.rs:1-8](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L1-L8) 声明 `pub mod bridge / server / tokenizers` 与 `pub(crate) mod utils`，proto 单独包进 `pub mod proto`。

文件分离写法的三处声明完全同构。以 server 为例：[src/server.rs:1009-1010](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L1009-L1010) 就是末尾两行

```rust
#[cfg(test)]
mod tests;
```

bridge 与 py_utils 完全一样：[src/bridge.rs:803-804](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L803-L804)、[src/utils/py_utils.rs:69-70](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/py_utils.rs#L69-L70)。

内联写法的代表是 request_utils：[src/utils/request_utils.rs:269-270](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L269-L270) 把 `#[cfg(test)]` 与 `mod tests {` 写在文件内部，测试体一直延续到 L342。

被测函数靠 `use super::{...}` 或 `use super::*;` 引入。例如 server 的测试文件顶部：[src/server/tests.rs:1-7](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server/tests.rs#L1-L7) 显式列出要测的 `DEFAULT_GRPC_MAX_MESSAGE_SIZE`、`resolve_max_message_size`、`terminal_error_status`、`openai_status_code`，并跨模块取 `crate::bridge::TerminalError`。

#### 4.1.4 代码实践

1. **目标**：确认你分得清两种写法，并理解测试模块挂在哪棵模块树上。
2. **步骤**：
   - 打开 [src/server.rs](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs) 滚到末尾，确认 `mod tests;` 解析到 `src/server/tests.rs`。
   - 打开 [src/utils/request_utils.rs](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs)，确认它的测试是内联 `mod tests { ... }`。
3. **观察现象**：`use super::*;` 在两种写法里指向的"父模块"分别是 `server` 与 `request_utils`。
4. **预期结果**：你能说出"如果要把 request_utils 的内联测试拆成独立文件，需要新建 `src/utils/request_utils/tests.rs`，并把 `mod tests { ... }` 替换为 `mod tests;`"。
5. 运行命令的产物：待本地验证（本 crate 是 `cdylib` + `extension-module`，见 4.2.4 的说明）。

#### 4.1.5 小练习与答案

**练习**：为什么这些测试用 `use super::{...}` 而不是 `use crate::...`？

**答案**：`super` 指向父模块，能访问父模块的**私有**项（如 `resolve_max_message_size`、`json_encode_string` 都是模块私有函数，没有 `pub`）。`crate::` 路径只能访问公开项。因为这些被测函数刻意没对外暴露，必须靠 `super` 才测得到。

---

### 4.2 串行 env 测试：`resolve_max_message_size_honors_env_var` 与 SAFETY 约定

#### 4.2.1 概念说明

`resolve_max_message_size` 读环境变量 `SGLANG_TONIC_PAYLOAD` 决定 gRPC 单条消息上限。要测它就得改环境变量，而**环境变量是进程级全局可变状态**——`cargo test` 默认多线程并行跑测试，若两个测试同时 `set_var`/`remove_var` 同一个变量，会互相覆盖、产生幽灵失败。

本 crate 的解法极具教学意义：**把同一个变量的所有用例捆绑进一个串行测试函数**，并在注释里写明 `SAFETY` 约定。这就是本模块标题里 `resolve_max_message_size_honors_env_var` 这条最小模块的全部价值。

#### 4.2.2 核心流程

被测函数 `resolve_max_message_size` 的判定逻辑（[src/server.rs:37-58](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L37-L58)）：

```text
读 SGLANG_TONIC_PAYLOAD
├─ Err(未设置)            → 默认 64 MiB
└─ Ok(raw)
   └─ raw.parse::<usize>()
      ├─ Ok(n) if n > 0   → 用 n（合法正数，原样采纳）
      └─ 其他(0 / 非数字)  → 告警 + 回退默认
```

默认值常量在 [src/server.rs:29-31](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L29-L31)，注释点明 64 MiB 是为多模态输入和 OpenAI JSON 透传体留余量，远高于 tonic 默认 4 MiB。这个值最终在 [src/server.rs:991-994](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L991-L994) 被装进 `max_decoding_message_size` / `max_encoding_message_size`。

测试覆盖四种子情形，**全部塞在一个 `#[test]` 函数里顺序执行**。

#### 4.2.3 源码精读

串行测试本体：[src/server/tests.rs:41-75](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server/tests.rs#L41-L75)。关键两段：

```rust
// SAFETY: env vars are process-global; bundle all SGLANG_TONIC_PAYLOAD cases
// into one serial test so they don't race each other under `cargo test`'s
// default parallelism.
#[test]
fn resolve_max_message_size_honors_env_var() {
    const VAR: &str = "SGLANG_TONIC_PAYLOAD";
    // Unset → default.
    unsafe { std::env::remove_var(VAR); }
    assert_eq!(resolve_max_message_size(), DEFAULT_GRPC_MAX_MESSAGE_SIZE);
    // Valid override → honored verbatim.
    unsafe { std::env::set_var(VAR, "1048576"); }
    assert_eq!(resolve_max_message_size(), 1_048_576);
    // Invalid string → warn + fall back to default.
    unsafe { std::env::set_var(VAR, "not-a-number"); }
    assert_eq!(resolve_max_message_size(), DEFAULT_GRPC_MAX_MESSAGE_SIZE);
    // Zero → treated as invalid, fall back to default.
    unsafe { std::env::set_var(VAR, "0"); }
    assert_eq!(resolve_max_message_size(), DEFAULT_GRPC_MAX_MESSAGE_SIZE);
    unsafe { std::env::remove_var(VAR); }
}
```

读法要点：

- 每次改完环境变量**立即**断言，绝不让两个 `set_var` 之间留空。
- 函数末尾 `remove_var` 收尾，避免污染后续测试——这是串行约定的一部分。
- `unsafe` 块不是错——本 crate 用 edition 2024 + 工具链 1.90（见 [rust-toolchain.toml](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/rust-toolchain.toml)），`std::env::set_var`/`remove_var` 在该版本下是 `unsafe` 的，根因正是"进程全局可变状态会数据竞争"——和上面要串行的理由是**同一个**。

#### 4.2.4 代码实践

1. **目标**：体会"若拆成多个并行测试会发生什么"。
2. **步骤**：在脑中把这个函数拆成 4 个独立 `#[test]`（`unset`、`valid`、`invalid_string`、`zero`），各自 `set_var` 后断言。
3. **观察现象**：并行执行时，`valid` 设的 `1048576` 可能在 `unset` 断言"未设置应得默认值"的瞬间仍然可见，导致 `unset` 测试随机失败。
4. **预期结果**：你能解释"串行 + 收尾清理"是这类 env 测试的最低成本正确写法。
5. **运行说明**：本 crate 是 `crate-type = ["cdylib"]` 且默认开启 `pyo3/extension-module`（见 [Cargo.toml:8-13](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/Cargo.toml#L8-L13) 与 `Cargo.toml:28-29`），它是作为 Python 扩展被打进 sglang wheel 的；项目的 Rust CI（`.github/workflows/pr-test-rust.yml`）里的 `cargo test` 是给 `sgl-model-gateway` 用的，并未单独跑 sglang-grpc 的单测。因此本地想直接 `cargo test` 验证时，可能需要走项目的 Python 构建链（setuptools-rust）或自行处理扩展模块链接——具体能否直接 `cargo test` 通过，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：把 `VAR` 设成 `"0"` 时为什么回退默认，而不是采纳 0？

**答案**：`Ok(n) if n > 0` 这个守卫把 0 排除在外。0 字节的消息上限无意义，会拒掉所有请求，所以视同非法值回退。

**练习 2**：如果未来要新增 `SGLANG_TONIC_PAYLOAD` 的又一个用例（比如负数），应该怎么改这个测试？

**答案**：在**同一个函数**里追加一段 `unsafe { set_var(...) }` + 断言，而不是新建一个 `#[test]`，以维持串行不变量。

---

### 4.3 纯函数单元测试：错误消息、JSON 编码与请求字典

#### 4.3.1 概念说明

本 crate 的测试几乎都是**纯函数测试**——不启 Tokio、不拿 GIL、不连真正的 gRPC server。这是有意为之的设计：被测函数（`TerminalError::message`、`json_encode_string`、`build_*_dict`）都被刻意写成无副作用、无 GIL、可单测的纯 Rust 函数。测试它们的性价比极高：快、确定、不依赖环境。

本模块覆盖三条最小模块线索：`terminal_error_messages_include_request_id`、`generate_dicts_include_session_id`，外加 py_utils 的 `fallback_string_is_json_encoded`。

#### 4.3.2 核心流程

这类测试统一遵循 **Arrange-Act-Assert**：

1. **Arrange**：构造输入。proto 消息用 `Default::default()` + 覆盖个别字段（prost 为 proto3 消息派生了 `Default`）。
2. **Act**：调用被测纯函数。
3. **Assert**：对返回值断言；需要时做一次**往返解码**（先编码再解码，验证可还原）。

#### 4.3.3 源码精读

**① bridge：错误消息必须含 rid。** [src/bridge/tests.rs:3-9](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge/tests.rs#L3-L9) 构造一个 `TerminalError::ClientDisconnected { rid: "rid" }`，断言 `message()` 里包含 `"rid"`。被测的 `message()` 在 [src/bridge.rs:55-67](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L55-L67)，三个变体都用 `format!("...{rid}...")` 把 rid 印进消息。这条断言的意义：gRPC 错误状态码的消息体要让运维能定位到具体请求。

**② request_utils：请求字典写入 session_id。** [src/utils/request_utils.rs:273-293](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L273-L293) 同时验证 `build_text_generate_dict` 与 `build_generate_dict` 两个函数都会把 `session_id` 写进字典：

```rust
let text_req = proto::TextGenerateRequest {
    session_id: session_id.clone(),
    ..Default::default()
};
assert_eq!(
    build_text_generate_dict("request-1", &text_req).get("session_id"),
    Some(&serde_json::json!("session-1"))
);
```

紧跟着还有两个"镜像"测试守卫条件插入：[request_utils.rs:295-328](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L295-L328) 验证 disaggregated 参数存在时三键齐全；[request_utils.rs:330-341](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L330-L341) 验证**缺失时这三个键根本不出现**——后者尤其重要，它守住"只插显式设置的字段，不污染 Python 默认值"的契约（详见 [u2-l7 请求字典构建](u2-l7-request-dict-building.md)）。

**③ py_utils：fallback 字符串要 JSON 编码。** [src/utils/py_utils/tests.rs:3-8](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/py_utils/tests.rs#L3-L8) 是一次往返：

```rust
let encoded = json_encode_string("<Foo at 0x123>");
let decoded: String = serde_json::from_str(&encoded).unwrap();
assert_eq!(decoded, "<Foo at 0x123>");
```

被测函数 `json_encode_string` 在 [src/utils/py_utils.rs:65-67](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/py_utils.rs#L65-L67)，它把任意字符串包成 `serde_json::Value::String` 再 `to_string()`，保证结果是一个合法的 JSON 字符串字面量（带引号、转义）。这个 fallback 路径在 [py_utils.rs:48-63](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/py_utils.rs#L48-L63) 的 `py_value_to_json_string` 里被用到——当 `json.dumps` 失败时兜底。

#### 4.3.4 代码实践

1. **目标**：把"读测试 → 预测输出"练成本能。
2. **步骤**：读 [src/utils/request_utils.rs:330-341](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L330-L341)，先不运行，写下你认为 `build_text_generate_dict("request-1", &TextGenerateRequest::default())` 返回的字典里是否含 `bootstrap_host`。
3. **观察现象**：对照被测函数 [request_utils.rs:111-160](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L111-L160)，注意 `insert_disaggregated_params` 只在 `Some` 时插入。
4. **预期结果**：默认请求的 `disaggregated_params` 是 `None`，所以三键都不出现，断言 `!contains_key(...)` 成立。
5. 运行验证：待本地验证（同 4.2.4 的构建链说明）。

#### 4.3.5 小练习与答案

**练习**：`fallback_string_is_json_encoded` 为什么不直接断言 `encoded == "\"<Foo at 0x123>\""`，而要先 `from_str` 解码再比？

**答案**：直接比对字面量脆弱——一旦 serde_json 的转义策略微调（比如对某些字符的转义方式变化），字面量比对就会误判失败。先解码再比，验证的是"语义可还原"这一真正想要的性质，更稳健。

---

### 4.4 分词器后端扩展点：`TokenizerBackend` trait 与 `load_backend`

#### 4.4.1 概念说明

[u2-l8](u2-l8-native-tokenizer.md) 讲过 `RustTokenizer` 的"原生优先、Python 回退"策略。本节聚焦它的**扩展点**：分词器后端被抽象成一个 crate 私有 trait `TokenizerBackend`，目前只有 `HuggingFaceTokenizerBackend` 一个实现，但留好了"加新后端"的钩子。这是"新增一个分词器后端"扩展场景的落点。

#### 4.4.2 核心流程

构造与回退链（[src/tokenizers.rs:54-96](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/tokenizers.rs#L54-L96)）：

```text
from_tokenizer_path(path, mode, context_len)
├─ resolve_tokenizer_json(path) 为 None → 返回 None（找不到 tokenizer.json）
├─ tokenizer_mode == Some("slow")       → 返回 None（强制走 Python 慢速分词）
└─ load_backend(tokenizer_json)
   ├─ Ok(backend) → 装进 RustTokenizer，返回 Some
   └─ Err(e)      → 告警，返回 None（回退 Python）
```

扩展点在 `load_backend`：[src/tokenizers.rs:113-118](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/tokenizers.rs#L113-L118)，注释直白写道"Add new native backend probes here"——新后端就是在这里加一行探测。

#### 4.4.3 源码精读

trait 定义只有三个方法，极简：[src/tokenizers.rs:5-9](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/tokenizers.rs#L5-L9)

```rust
trait TokenizerBackend: Send + Sync {
    fn name(&self) -> &'static str;
    fn encode(&self, text: &str, add_special_tokens: bool) -> Result<Vec<u32>, String>;
    fn decode(&self, ids: &[u32], skip_special_tokens: bool) -> Result<String, String>;
}
```

`Send + Sync` 约束让它能被跨线程共享（`PyBridge` 会持有它）。错误类型故意用 `String` 而非自定义 error，保持轻量。

唯一实现 `HuggingFaceTokenizerBackend`：[src/tokenizers.rs:11-41](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/tokenizers.rs#L11-L41)，包装 HuggingFace `tokenizers` crate 的 `Tokenizer`，`name()` 返回 `"huggingface-tokenizers"`。

`RustTokenizer` 持有一个 trait object：[src/tokenizers.rs:47-49](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/tokenizers.rs#L47-L49) 字段是 `backend: Box<dyn TokenizerBackend>`。所有 encode/decode 都委托给 `self.backend`，`RustTokenizer` 本身不知道后端具体是谁。

`resolve_tokenizer_json` 决定"文件在哪"：[src/tokenizers.rs:120-127](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/tokenizers.rs#L120-L127)，若传入的是文件就直接用，否则拼 `tokenizer.json`，再 `.exists()` 判存在。

#### 4.4.4 代码实践

1. **目标**：追踪回退的三道 `None` 关卡，并为"加占位后端"做准备。
2. **步骤**：
   - 读 [tokenizers.rs:54-96](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/tokenizers.rs#L54-L96)，列出会让 `from_tokenizer_path` 返回 `None` 的三种情况。
   - 读 [tokenizers.rs:113-118](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/tokenizers.rs#L113-L118)，思考"新后端探测失败时该返回什么"。
3. **观察现象**：`load_backend` 目前只调一次 `HuggingFaceTokenizerBackend::from_file`，没有按文件特征（如 magic header）路由后端——这是留给扩展者的空间。
4. **预期结果**：你能说出"新增后端 = 加一个 impl 了 `TokenizerBackend` 的结构体 + 在 `load_backend` 里加一行探测；探测失败应返回 `Err`，这样上层才会回退 Python"。
5. 运行验证：待本地验证。

#### 4.4.5 小练习与答案

**练习**：为什么 `TokenizerBackend` 是 crate 私有 trait，而 `RustTokenizer` 是 `pub` 的？

**答案**：对外（Python 桥接层）只暴露"一个能 encode/decode 的分词器"这一稳定接口（`RustTokenizer`），后端的具体类型与多派发机制是内部实现细节，不该泄漏。这样新增/替换后端不会影响外部调用方，符合"封装变化点"。

---

### 4.5 扩展实践：新增一个 gRPC RPC 的完整清单

#### 4.5.1 概念说明

sglang-grpc 的服务由 proto 契约驱动（见 [u2-l1 proto 契约与代码生成](u2-l1-proto-contract-and-codegen.md)）。新增一个 RPC 会牵动多层，但**不是每层都要手改**。本节用真实的 `FlushCache` RPC 当作范本，给出一份完整改动清单。这是本讲最实用的一块。

#### 4.5.2 核心流程

新增一个最简单的**一元（unary）RPC**，按层从上到下：

| 序号 | 文件 | 改动 | 是否必改 |
| --- | --- | --- | --- |
| 1 | `proto/.../sglang.proto` | 加 `message XxxRequest{}` / `XxxResponse{...}`，在 `service SglangService` 里加一行 `rpc Xxx(XxxRequest) returns (XxxResponse);` | **必改** |
| 2 | `build.rs` | 无需手改——`tonic_build` 全文件编译 + `rerun-if-changed` 会在 proto 变化时自动重新生成 trait | **免改** |
| 3 | `src/server.rs` | 在 `impl SglangService for SglangServiceImpl` 块里加一个 `async fn xxx(...)`，签名由生成器决定 | **必改** |
| 4 | `src/utils/request_utils.rs` | 仅当是"数据型" RPC（要把 proto 字段翻译给 Python）时，加一个 `build_xxx_dict` | 视情况 |
| 5 | `src/bridge.rs` | 仅当需要新建通道提交请求时，加一个 `submit_xxx`；控制型可直接调 runtime_handle | 视情况 |
| 6 | 错误映射 | 复用现成的 `pyerr_to_status` / `recv_json_response`，无需新增映射 | **免改** |

#### 4.5.3 源码精读

**① 契约层。** proto 服务块 [proto/sglang/runtime/v1/sglang.proto:4-35](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L4-L35) 里，`FlushCache` 是一元 RPC（无 `stream` 关键字）：[proto:19](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L19)。其请求/响应消息在 [proto:247-252](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L247-L252)。

**② 实现层。** tonic 生成的 trait 要求 server.rs 里实现 `async fn flush_cache`：[src/server.rs:676-694](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L676-L694)

```rust
async fn flush_cache(
    &self,
    _request: Request<proto::FlushCacheRequest>,
) -> Result<Response<proto::FlushCacheResponse>, Status> {
    let rid = uuid::Uuid::new_v4().to_string();
    let receiver = self
        .bridge
        .submit_flush_cache(&rid)
        .map_err(|e| pyerr_to_status(e, "Failed to flush cache"))?;
    let json_str =
        recv_json_response(&self.bridge, &rid, receiver, self.response_timeout).await?;
    let v: serde_json::Value = serde_json::from_str(&json_str)
        .map_err(|e| Status::internal(format!("Failed to parse JSON response: {}", e)))?;
    Ok(Response::new(proto::FlushCacheResponse {
        success: v["success"].as_bool().unwrap_or(false),
        message: v["message"].as_str().unwrap_or("").to_string(),
    }))
}
```

注意三个复用点（无需新造）：

- 错误映射直接 `pyerr_to_status`（[server.rs:66-76](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L66-L76)）把 Python 异常翻成 gRPC `Status`。
- 一元收尾直接 `recv_json_response` 收终止 chunk 再解析 JSON。
- proto 必填字段用 `unwrap_or` 兜底默认值（`success` 默认 `false`、`message` 默认 `""`）。

trait 实现块入口在 [server.rs:216-217](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L216-L217)。

**③ 为什么 build.rs 免改。** u1-l2 讲过 `build.rs` 用 `tonic_build::configure()` 全文件编译 proto，且只对 proto 设了 `rerun-if-changed`。改 proto 会自动触发重新生成 server trait，新 RPC 的方法签名会被加进 trait，server.rs 若不实现就会编译报错——这恰好"逼"你完成第 3 步。

#### 4.5.4 代码实践

1. **目标**：为一个假想的"最简一元 RPC"列出每层具体改动。
2. **场景**：新增 `rpc Ping(PingRequest) returns (PingResponse);`，其中 `PingResponse { string message = 1; }`，服务端返回固定字符串 `"pong"`。
3. **步骤**：按上表逐文件写出你要加的代码片段（proto 加 message+rpc；server.rs 加 `async fn ping` 直接 `Ok(Response::new(PingResponse { message: "pong".into() }))`）。
4. **观察现象**：因为 `ping` 不需要调 Python、不需要建通道，所以第 4、5 步可省略——这是"控制型 RPC 比数据型 RPC 省事"的体现。
5. **预期结果**：你能说清"这个 RPC 不需要 `build_xxx_dict`、不需要 `submit_xxx`、不需要新错误映射"。
6. 运行验证：待本地验证（修改 proto 后需走项目构建链重新生成代码）。

> ⚠️ 注意：本讲只读不改。上面是"设计清单"，不要真的去改 proto 或 server.rs。

#### 4.5.5 小练习与答案

**练习**：如果你忘了在 server.rs 实现 `flush_cache`，会怎样？

**答案**：`SglangServiceImpl` 将不再满足 tonic 生成的 `SglangService` trait（trait 里多了 `flush_cache` 方法），编译期直接报错 `error[E0046]: missing ... flush_cache in implementation`。这是契约驱动的好处——proto 是单一事实源，实现漏了立刻编译失败。

---

## 5. 综合实践

本次综合实践给你**两个二选一**的任务，任选其一完成。两个任务都直接对应本讲的扩展主题。

### 任务 A（推荐）：为 `build_text_embed_dict` 补写单元测试

**背景**：[src/utils/request_utils.rs:211-226](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L211-L226) 的 `build_text_embed_dict` 目前**没有任何测试**。它的逻辑是：写 `rid`/`text`，按需写 `routing_key`，非空才写 `external_trace_header`，最后盖 `received_time`。

**目标**：仿照 [request_utils.rs:273-293](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L273-L293) 的风格，写两个测试，断言：
- `routing_key` 与非空 `trace_headers` 被正确写入；
- 空 `trace_headers` 时**不出现** `external_trace_header` 键。

**参考答案代码**（示例代码，需放进 `src/utils/request_utils.rs` 的 `#[cfg(test)] mod tests` 块里）：

```rust
// 示例代码：为 build_text_embed_dict 补写测试
#[test]
fn text_embed_dict_includes_routing_key_and_trace_headers() {
    let req = proto::TextEmbedRequest {
        text: "hello".to_string(),
        rid: Some("rid-1".to_string()),
        routing_key: Some("route-42".to_string()),
        trace_headers: HashMap::from([("trace-id".to_string(), "abc".to_string())]),
    };

    let d = build_text_embed_dict("rid-1", &req);

    // routing_key 原样写入
    assert_eq!(d.get("routing_key"), Some(&serde_json::json!("route-42")));
    // rid / text 总是写入
    assert_eq!(d.get("rid"), Some(&serde_json::json!("rid-1")));
    assert_eq!(d.get("text"), Some(&serde_json::json!("hello")));
    // 非空 trace_headers → external_trace_header 是整张 map 的 JSON
    let trace = d
        .get("external_trace_header")
        .expect("trace headers should be present");
    let parsed: HashMap<String, String> = serde_json::from_value(trace.clone()).unwrap();
    assert_eq!(parsed.get("trace-id"), Some(&"abc".to_string()));
}

#[test]
fn text_embed_dict_omits_external_trace_header_when_empty() {
    let req = proto::TextEmbedRequest {
        text: "hello".to_string(),
        trace_headers: HashMap::new(),
        ..Default::default()
    };

    let d = build_text_embed_dict("rid-1", &req);

    assert!(!d.contains_key("external_trace_header"));
    // routing_key 缺省时同样不应出现
    assert!(!d.contains_key("routing_key"));
}
```

**自检要点**：

1. `TextEmbedRequest` 的字段来自 [proto:121-126](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L121-L126)（`text` 必填、`rid`/`routing_key` 为 `optional`、`trace_headers` 是 `map`）。
2. `..Default::default()` 能用，因为 prost 为 proto3 消息派生了 `Default`（现有测试 `generate_dicts_include_session_id` 已这样用）。
3. 你需要确认 `HashMap` 已在测试模块作用域内——`request_utils.rs` 顶部 `use std::collections::HashMap;`，内联 `mod tests` 用 `use super::*;` 即可拿到。

**运行**：待本地验证（见 4.2.4 的构建链说明）。

### 任务 B（备选）：为 `TokenizerBackend` 新增一个占位后端

**目标**：仿照 `HuggingFaceTokenizerBackend`（[tokenizers.rs:11-41](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/tokenizers.rs#L11-L41)），新增一个永远探测失败的占位后端，并在 `load_backend`（[tokenizers.rs:113-118](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/tokenizers.rs#L113-L118)）里注册。

**参考答案代码**（示例代码）：

```rust
// 示例代码：占位后端，演示 load_backend 探测链如何扩展
struct PlaceholderTokenizerBackend;

impl PlaceholderTokenizerBackend {
    fn try_probe(_path: &Path) -> Result<Self, String> {
        Err("placeholder backend not implemented".to_string())
    }
}

impl TokenizerBackend for PlaceholderTokenizerBackend {
    fn name(&self) -> &'static str {
        "placeholder"
    }
    fn encode(&self, _: &str, _: bool) -> Result<Vec<u32>, String> {
        Err("placeholder backend cannot encode".to_string())
    }
    fn decode(&self, _: &[u32], _: bool) -> Result<String, String> {
        Err("placeholder backend cannot decode".to_string())
    }
}

fn load_backend(tokenizer_json: &Path) -> Result<Box<dyn TokenizerBackend>, String> {
    // Add new native backend probes here. ...
    if let Ok(b) = PlaceholderTokenizerBackend::try_probe(tokenizer_json) {
        return Ok(Box::new(b) as Box<dyn TokenizerBackend>);
    }
    HuggingFaceTokenizerBackend::from_file(tokenizer_json)
        .map(|backend| Box::new(backend) as Box<dyn TokenizerBackend>)
}
```

**自检要点**：

1. 占位后端探测失败应返回 `Err`，让链路继续尝试下一个后端（或最终回退 Python）。
2. `Send + Sync` 约束天然满足（无字段的单元结构体）。
3. 这只是练手骨架——真正的新后端需要在 `try_probe` 里按文件特征（如 magic header / 文件名）判断是否归属本后端。

> ⚠️ 本讲只读不改源码。以上两段代码是供你在自己的练习副本里尝试的"示例代码"，不要写入仓库的 `src/`。

## 6. 本讲小结

- sglang-grpc 的测试用 `#[cfg(test)] mod tests` 紧贴实现，**文件分离**（`mod tests;`）与**内联**（`mod tests { ... }`）两种写法混用，靠 `use super::*` 访问模块私有项。
- 改进程级环境变量的测试（`resolve_max_message_size_honors_env_var`）必须**串行**，把同一变量的所有用例捆进一个函数；edition 2024 下 `set_var`/`remove_var` 是 `unsafe`，根因同样是"进程全局可变状态"。
- 被测函数被刻意设计成**无 GIL、无 Tokio、无副作用**的纯函数（`TerminalError::message`、`json_encode_string`、`build_*_dict`），测试快而确定，常做"往返解码"断言。
- 分词器后端靠 crate 私有 trait `TokenizerBackend` + `Box<dyn ...>` 做多派发，`load_backend` 是唯一的探测链扩展点（"Add new native backend probes here"）。
- 新增一个 gRPC RPC 的改动清单：proto 必改、server.rs 必改、请求字典/桥接提交视 RPC 类型而定、错误映射可复用、**build.rs 免改**（契约驱动自动重生）。
- 本 crate 是 `cdylib` Python 扩展，单测通常随 sglang wheel 一起构建，直接 `cargo test` 的可行性待本地验证。

## 7. 下一步学习建议

本讲是 sglang-grpc 学习手册的终篇。建议接下来：

1. **横向对照**：拿本讲的"新增 RPC 清单"去重读 [u2-l2 服务实现总览](u2-l2-service-impl-overview.md) 与 [u2-l4 一元 RPC 与 JSON 解析](u2-l4-unary-rpcs-and-json.md)，确认你对三类 RPC 的公共提交模式（`submit_request` / `submit_json` / `submit_openai`）已能自如选用。
2. **补测练习**：把综合实践任务 A 落地，再尝试为 `build_classify_dict`（[request_utils.rs:247-267](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L247-L267)）补一个"同时给 `text` 与 `input_ids` 时两者都写入"的测试。
3. **跨语言回看**：从 Python 侧重新审视整条链路——读 `python/sglang/srt/entrypoints/grpc_server.py` 与 `http_server.py` 里调用 `start_server` 的位置，把"Python RuntimeHandle → Rust PyBridge → tonic → 回调 → Python"的闭环在脑子里走一遍，检验整个手册的成果。
4. **关注上游**：留意 server.rs 里 `resolve_max_message_size` 上方的 `TODO(grpc-args)`（[server.rs:33-36](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L33-L36)）与 `run_grpc_server` 上方的 `TODO(grpc-auth)`（[server.rs:975-977](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L975-L977)），这两个是本 crate 公开演进的方向。
