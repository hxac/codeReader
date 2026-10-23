# 构建、依赖与测试：把 crate 跑起来

> 前置讲义：u1-l1（项目定位与架构全景）、u1-l2（目录结构与模块地图）。
> 本讲承接 u1-l1 定下的两条硬边界——「CUDA 内核编译归 pegainfer-kernels 的 build.rs」与「core 自己没有 build.rs」——把它们落到可执行的构建与测试命令上。

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 [pegainfer-core/Cargo.toml](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml#L1-L39) 的每一个段落，并能从 22 个运行时依赖反推出各模块的职责分工。
2. 用 `cargo test --release -p pegainfer-core --lib` 跑通 core 的内嵌单元测试，并解读输出的通过/失败/过滤数量。
3. 说清这个 crate 里 GPU 测试的**真实**隔离机制——它不是你可能想象的 `#[ignore]`，而是「分层命令 + 显式 panic」——并知道无 GPU 机器上哪些测试会失败、为什么。
4. 分清 `--lib`、`--test <名字>`、`--ignored` 三种测试入口各自会命中哪些代码，以及 `PEGAINFER_*` 环境变量分别服务于谁。

## 2. 前置知识

- **Cargo workspace（工作区）**：多个 crate 共享一个根 `Cargo.toml`，依赖版本集中在 `[workspace.dependencies]` 表里声明一次，成员 crate 用 `dependency = { workspace = true }` 引用。好处：全仓库版本一致，升级只改一处。
- **`#[cfg(test)]`（条件编译）**：Rust 的单元测试惯例是写在源文件末尾的 `#[cfg(test)] mod tests { ... }` 里。`cfg` 是**编译期**开关：`cargo build --release` 产出的二进制里根本没有这段代码，零运行时开销。
- **`#[ignore]` 属性**：Rust 测试框架提供的机制，被标注的测试默认跳过，需要 `cargo test -- --ignored` 显式触发。本讲会证明 core 里**一个都没用**——这是很多读者（包括本手册大纲的规划者）容易想当然的地方。
- **`--release`**：优化构建。对 GPU/CUDA 项目几乎是强制项，CLAUDE.md 明确写着 debug 构建「extremely slow … will timeout」。
- **doctest（文档测试）**：rustdoc 会把 `///` 注释里的 ```` ```rust ```` 代码块当测试编译运行；标了 ```` ```ignore ```` 的代码块不参与运行。
- **前置讲义术语回顾**：prefill/decode、KV cache、RAII、门面（tensor.rs/ffi.rs 各一行转发 kernels）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它做什么 |
|---|---|---|
| `pegainfer-core/Cargo.toml` | core 的 crate 清单（包元数据、依赖、feature、lints） | 4.1 依赖图的主角 |
| `Cargo.toml`（仓库根） | workspace 清单：集中管理依赖版本与 clippy lint 等级 | 4.1 解释 `workspace = true` 的解析去向 |
| `pegainfer-core/src/page_pool.rs` | 固定页分配器（纯 CPU 数据结构） | 4.2 内嵌测试的第一样本：3 个纯 CPU 测试 |
| `pegainfer-core/src/kv_pool.rs` | KV 池：布局几何（纯 CPU）+ GPU 缓冲（需 GPU） | 4.2/4.3 对比「同一文件里两类测试」 |
| `pegainfer-core/tests/weight_loader_gpu.rs` | 唯一的集成测试文件，自述「GPU, model-free」 | 4.3 讲 `--lib` 与集成测试的边界 |
| `CLAUDE.md` | 工作区构建/测试约定与环境变量总表 | 4.3 的命令与环境变量出处 |
| `pegainfer-core/src/tracing.rs` | 追踪设施 | 4.3 顺带确认 core 自己读的唯一 `PEGAINFER_*` 环境变量 |
| `pegainfer-core/src/weight_loader.rs` | 权重加载 | 4.3 顺带确认 crate 里唯一的 ```` ```ignore ```` 文档示例 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**Cargo 依赖图 → 内嵌单元测试 → GPU 测试隔离约定**。三者正好是「把 crate 跑起来」的三个追问：它由什么构成？它的测试长在哪？测试怎么分快慢车道？

### 4.1 Cargo 依赖图：从 crate 清单读懂一个 crate

#### 4.1.1 概念说明

`Cargo.toml` 是 crate 的「身份证 + 采购单」。对一个你还没读过的 crate，先看清单往往比先看代码更快建立全局感：依赖列表直接暴露了它「借了谁的力」，也就暴露了它的模块大概在做什么。

对 pegainfer-core 还有一层特殊意义：u1-l1 确认过 core **没有 build.rs**，CUDA 编译责任整体委托给 `pegainfer-kernels`。这意味着「构建 core」实际上隐含「先构建 kernels（跑 nvcc）」——这一点会直接决定你的测试命令能不能在一台机器上跑通（见 4.3）。

#### 4.1.2 核心流程

读一份 Cargo.toml 的固定动线：

1. `[package]` → 身份：名字、版本、edition、许可证。
2. `[dependencies]` → 采购单：逐个问「它被谁用、用在哪」。
3. `[dev-dependencies]` → 只在测试/示例编译时引入的依赖。
4. `[features]` → 可选功能开关，注意 `default` 里有什么。
5. `[lints]` → 是否继承 workspace 的 lint 等级。

core 的 22 个运行时依赖按用途可以分为六组（这是本讲建议你背下来的心智模型）：

| 分组 | 依赖 | 对应模块（源码证据） |
|---|---|---|
| GPU / 数值底座 | `cudarc`、`half`、`pegainfer-kernels` | tensor.rs（CUDA driver 绑定）、kv_pool.rs（`bf16`、`CudaSlice`） |
| 错误处理 | `anyhow` | 全 crate 的 `Result`/`bail!` |
| 并发 | `parking_lot`、`rayon` | page_pool.rs 的 `Mutex`、staging.rs 的线程池 |
| 权重文件 | `memmap2`、`safetensors`、`serde`、`serde_json` | weight_loader.rs |
| 日志 / 追踪 | `log`、`logforth`、`colored`、`chrono`、`fastrace`、`fastrace-opentelemetry`、`opentelemetry`、`opentelemetry-otlp`、`opentelemetry_sdk`、`libc` | logging.rs、tracing.rs |
| 服务面 | `pegainfer-frontend`、`tokio` | tracing.rs（仅一处联动） |

#### 4.1.3 源码精读

**crate 身份**：edition 2024、Apache-2.0、版本 0.1.0——

- [pegainfer-core/Cargo.toml:L1-L5](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml#L1-L5)：包名、edition、许可证、版本号。

**采购单与 dev 依赖**：

- [pegainfer-core/Cargo.toml:L7-L29](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml#L7-L29)：22 个运行时依赖，全部 `{ workspace = true }`——版本不在本文件里，统一去根 Cargo.toml 查。
- [pegainfer-core/Cargo.toml:L31-L32](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml#L31-L32)：`tempfile` 是唯一的 dev-dependency，只在编译测试时引入，不会进入发布产物。

**「依赖被谁用」的三条源码证据**（上表的抽样验证）：

- [pegainfer-core/src/page_pool.rs:L3](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L3)：`use parking_lot::Mutex;`——页池的 free-list 锁来自 parking_lot（比 std 的 Mutex 更快、不可重入）。
- [pegainfer-core/src/kv_pool.rs:L5-L8](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L5-L8)：`cudarc::driver::CudaSlice`（GPU 显存缓冲）、`half::bf16`（半精度类型），并且直接 `pub use` 了 kernels 的 `KvFormat`/`KvStorage`——这就是 u1-l1 说的「门面」在依赖层的体现。
- [pegainfer-core/src/weight_loader.rs:L22-L24](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/weight_loader.rs#L22-L24)：`memmap2::Mmap` + `safetensors`——权重加载 = 内存映射 + safetensors 视图。

**workspace 解析的去向**（根 Cargo.toml 的对应条目）：

- [Cargo.toml:L130](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L130)：`pegainfer-kernels = { path = "pegainfer-kernels" }`——唯一的路径依赖（工作区内兄弟 crate），其余如 [anyhow（L49）](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L49)、[cudarc（L78）](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L78)、[parking_lot（L152）](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L152)、[safetensors（L166）](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L166)、[tempfile（L170）](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L170) 都是版本依赖。

**feature 与 lint 等级**：

- [pegainfer-core/Cargo.toml:L34-L36](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml#L34-L36)：core 自己只有一个 feature——`kernel-call-trace`，且 `default = []` 意味着默认构建不含任何开关（模型 crate 的 feature 如 `qwen3`、`qwen35` 都在各自 crate 里，不在 core）。
- [pegainfer-core/Cargo.toml:L38-L39](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml#L38-L39) + [Cargo.toml:L199-L200](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/Cargo.toml#L199-L200)：core 继承 workspace 的 `clippy::pedantic = warn`——这就是这个 crate 代码风格「注释密度高、类型标注严谨」的制度来源之一。

> 一个诚实的提醒：本讲规格（以及很多凭印象的描述）会列出 `thiserror` 之类的依赖——**实际清单里没有它**，core 的错误处理统一走 `anyhow`。读清单，别读记忆。

#### 4.1.4 代码实践：把采购单画成图

1. **实践目标**：不看源码，仅凭 Cargo.toml 说出每个依赖的消费者；再用工具验证。
2. **操作步骤**：
   - 在仓库根运行 `cargo tree -p pegainfer-core --depth 1 > /tmp/core-deps.txt`（输出待本地验证）。
   - 对照上面六组分类表，把输出里的 22 个依赖逐个归组；归不进的记入「待确认」。
   - 任意挑三个依赖，用 `grep -rn "use <依赖名>" pegainfer-core/src` 找到它们的 import 位置，填进你的分组表第三列。
3. **需要观察的现象**：`cargo tree` 的顶层条目与 Cargo.toml `[dependencies]` 一一对应；`pegainfer-kernels` 作为 path 依赖出现在树里。
4. **预期结果**：得到一张「依赖 → 消费模块」的完整对照表；本讲 4.1.3 已给出三条样例（parking_lot→page_pool、cudarc/half→kv_pool、memmap2/safetensors→weight_loader）。
5. 若本地没有 Rust 工具链，替代方案是纯阅读：以 [pegainfer-core/Cargo.toml:L7-L29](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml#L7-L29) 为左栏、`grep use` 结果为右栏完成同一张表。

#### 4.1.5 小练习与答案

**练习 1**：core 的依赖里哪一个是本地路径依赖？它为什么必须是路径依赖？
**答案**：`pegainfer-kernels`（根 Cargo.toml L130，`path = "pegainfer-kernels"`）。因为 core 直接复用 kernels 编译产出的类型与函数（如 `paged_kv::derive_strides`、`KvFormat`），两者必须同仓库同步演进，不发布到 crates.io。

**练习 2**：`kernel-call-trace` feature 在默认构建里生效吗？怎么打开？
**答案**：不生效，`default = []`（Cargo.toml L34-L35）。需要 `cargo test --release -p pegainfer-core --features kernel-call-trace` 显式开启。它只影响 core 自己的代码路径，与模型 crate 的 feature（`qwen35` 等）互不相干。

**练习 3**：`tempfile` 和 `parking_lot` 的编译时机有什么区别？
**答案**：`tempfile` 是 dev-dependency（L31-L32），只在编译测试/benchmark/example 目标时引入，正常 `cargo build --release` 完全不碰它；`parking_lot` 是运行时依赖，会进入所有产物。

### 4.2 内嵌单元测试：`#[cfg(test)]` 的就地测试风格

#### 4.2.1 概念说明

pegainfer-core 没有独立的单测目录——**单元测试就写在被测代码的同一个文件里**，位于文件末尾的 `#[cfg(test)] mod tests` 中。这个风格的好处是：测试与实现同屏演进，重构时不容易「忘了改测试」；代价是源文件偏长。

更细的一层：这个 crate 连**仅供测试使用的方法**也用 `#[cfg(test)]` 单独标注（函数级裁剪），发布构建里这些方法不存在。

统计一下规模：`src/` 下共有 **43 个 `#[test]` 函数，分布在 12 个文件**（page_pool、kv_pool、logging、cuda_graph/dump、cuda_graph/dump/render、eos、decodable_vocab、ops/call_trace、rope、weight_loader/staging、weight_loader、cpu_topology）。注意 `src/tensor.rs` 与 `src/ops.rs` 本体不在其中——它们的测试压力由模型 crate 侧的精度门承担（u5-l2 展开）。

#### 4.2.2 核心流程

内嵌测试的生命周期：

```text
cargo test --release -p pegainfer-core --lib
  → 编译 lib 目标（含 #[cfg(test)] 代码，此时 test cfg 开启）
  → 逐文件收集 mod tests 里的 #[test] 函数 → 生成测试 harness
  → 运行；每个 #[test] 独立 panic 边界，失败互不传染
  （cargo build --release 时 test cfg 关闭，mod tests 整体不参与编译）
```

读一个池类测试的关键，是理解 free-list 的**后进先出**不变量：

- `PagePool::new(n)` 把 free-list 初始化为 `[n-1, …, 1, 0]`（倒序收集，见 L36），`pop()` 从尾部取——所以先分配到的是 `PageId(0)`、`PageId(1)`……页号按「从小到大」出借。
- `OwnedPagePermit::drop` 把持有的页**逆序**塞回尾部（L128 的 `.rev()`），恰好让「全释放后再全量申请」得到 `[0,1,2,3]` 的稳定顺序——测试断言就锚定在这个可复现顺序上。

#### 4.2.3 源码精读

**page_pool.rs：3 个纯 CPU 测试。** 页池本体只是一个 `Mutex<Vec<PageId>>` 计数器（[page_pool.rs:L11-L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L11-L15)），不含任何 GPU 资源，所以它的测试在任意机器上都能跑：

- [page_pool.rs:L132-L133](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L132-L133)：`#[cfg(test)] mod tests` 模块声明——内嵌测试的入口样板。
- [page_pool.rs:L137-L163](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L137-L163)：`acquires_pages_and_restores_on_drop` 借作用域 `{ … }` 模拟 permit 生命周期：出借 2+1 页后池剩 1，作用域结束 drop 归还，池回到 4——这是 RAII 归还语义的直接验证（u3-l1 会展开 absorb/try_grow）。
- [page_pool.rs:L165-L171](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L165-L171)：容量不足时 `try_acquire_many` 返回 `None` 而不是 panic——「部分失败」走 Option 的证据。
- [page_pool.rs:L173-L193](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L173-L193)：`try_grow` 扩张成功/越界失败两条路径，失败时 permit 保持不变（原子性）。
- [page_pool.rs:L120-L130](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L120-L130)：`Drop for OwnedPagePermit`——测试断言背后的实现本体，注意 L128 的 `.rev()`。

**kv_pool.rs：同一文件里住着两类测试（本讲的关键对照）。**

- [kv_pool.rs:L448-L456](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L448-L456)：测试模块与 `test_pool` 辅助函数——后者第一行就是 `DeviceContext::new().expect("GPU required for kv_pool tests")`，GPU 依赖在辅助函数里显式声明。
- [kv_pool.rs:L473-L493](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L473-L493)：**两个 stride 几何测试，纯 CPU**。它们只构造 `KvLayout`，而 `KvLayout` 的文档注释明确写着「Pure value type — no GPU, no allocation」（[kv_pool.rs:L15-L18](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L15-L18)）。以 Qwen3.5-4B 为例，断言的就是三重乘积：

  \[
    \text{page\_stride} = \text{num\_layers} \times \underbrace{2 \times \text{page\_size} \times \text{num\_kv\_heads} \times \text{head\_dim}}_{\text{layer\_stride（K 与 V 各一块）}}
  \]

  代入 8 层、4 头、head_dim 256、页 16：\(16 \times 4 \times 256 = 16384\)（kv_block_len），\(2 \times 16384 = 32768\)（layer_stride），\(8 \times 32768 = 262144\)（page_stride，×2 字节 = 512 KB/页）。Qwen3-4B 那条则验证 \(36 \times 2 \times 16 \times 4 \times 128 = 589824\)。
- [kv_pool.rs:L495-L538](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L495-L538)：`kv_state_lifecycle` 需要 GPU（经 `test_pool` 建真实缓冲），完整走一遍 prefill 10 → 逐 token 解码到 16 → 跨页到 17 → `reset()` 归还，是 u3-l2 的预演。
- [kv_pool.rs:L540-L548](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L540-L548)：`kv_state_out_of_pages`——「恰好装满 32 成功、第 33 个 token 失败」的边界测试。
- [kv_pool.rs:L550-L562](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L550-L562)：drop 归还页（RAII 在 KV 层的复验）。

**函数级 `#[cfg(test)]`——只为测试存在的方法：**

- [kv_pool.rs:L410-L420](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L410-L420)：`KvState::reset` 带 `#[cfg(test)]`——生产代码不重置请求状态（每个请求新开一个 `alloc()`），只有测试需要循环利用。
- [kv_pool.rs:L437-L440](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L437-L440)：`KvDesc::num_pages` 同样只在测试编译——它是给断言用的只读窥探口。

#### 4.2.4 代码实践：按名过滤跑一个文件的测试

1. **实践目标**：只运行 page_pool 的 3 个测试，验证 free-list 顺序不变量。
2. **操作步骤**：在仓库根执行 CLAUDE.md 推荐的过滤式命令（[CLAUDE.md:L73](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md#L73)）：

   ```bash
   cargo test --release -p pegainfer-core --lib page_pool -- --nocapture
   ```

3. **需要观察的现象**：测试名含 `page_pool` 的过滤器命中 3 个测试（`acquires_pages_and_restores_on_drop`、`reports_insufficient_capacity_with_none`、`try_grow_extends_permit_and_returns_all_on_drop`）。
4. **预期结果**：`test result: ok. 3 passed; 0 failed`（具体计数待本地验证；此命令需先成功构建 pegainfer-kernels，见 4.3）。
5. **无构建环境时的替代（源码阅读型）**：对照 [page_pool.rs:L36](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L36) 与 [page_pool.rs:L128](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L128)，手推 `PagePool::new(4)` 在「acquire 2 → drop → acquire 4」序列下 free-list 的每一步内容，验证它最终是 `[3,2,1,0]` 且出借序为 `[0,1,2,3]`——这正是 L159-L162 断言的来源。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cargo build --release` 的产物里完全没有测试代码，却能说「测试与实现同文件」没有运行时代价？
**答案**：`#[cfg(test)]` 是编译期条件编译，非测试构建中整个 `mod tests`（以及函数级的 test-only 方法如 `reset`）根本不参与编译，二进制里不存在这些符号。

**练习 2**：手算 `PagePool::new(4)` 之后 free-list 的内容；随后 `try_acquire_many(2)` 拿到哪些页？
**答案**：初始化为 `[3,2,1,0]`（`(0..4).rev()` 收集）；`pop()` 从尾部取，先得到 `PageId(0)` 再 `PageId(1)`，即测试断言的 `&[PageId(0), PageId(1)]`，池剩 2 页可用。

**练习 3**：`stride_geometry_qwen35` 为什么可以在没有 GPU 的机器上通过？
**答案**：它只调用 `KvLayout::new`，而 `KvLayout` 是纯值类型（文档注释原话「no GPU, no allocation」），stride 由 `pegainfer_kernels::paged_kv::derive_strides` 纯算术推导（[kv_pool.rs:L68-L100](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L68-L100)），不需要任何 CUDA 上下文。

### 4.3 GPU 测试的真实隔离约定（并修正一个常见误解）

#### 4.3.1 概念说明

先说结论：**pegainfer-core 里没有任何 `#[ignore]` 标注的测试。** 在整个 crate 范围 grep `ignore`，命中的只有三处不相干的东西：两处 `eq_ignore_ascii_case`（字符串比较 API）和一处文档示例的 ```` ```ignore ````。所以「GPU 测试被 `#[ignore]` 隔离」这个想当然的描述在本 crate 不成立——真实的隔离是三层约定叠加：

1. **命令分层**：快速车道 `cargo test --release --workspace --lib`（CLAUDE.md 标注约 9 秒）只跑各 crate 的 lib 单测；GPU 精度/集成测试按名字单独跑（`--test hf_golden_gate` 等，见 [CLAUDE.md:L64-L70](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md#L64-L70)）。
2. **目标分层**：`--lib` 天然不编译 `tests/` 目录。core 的集成测试 `tests/weight_loader_gpu.rs` 文件头自述「GPU, model-free」——它需要 GPU 但**不需要模型权重**，必须用 `cargo test --release -p pegainfer-core --test weight_loader_gpu` 单独触发。
3. **运行时显式失败**：真正需要 GPU 的 lib 单测（kv_pool 的 4 个）是普通 `#[test]`，在第一步就 `DeviceContext::new().expect("GPU required for kv_pool tests")`——无 GPU 的机器上它们**红灯失败**而不是静默跳过。与 `#[ignore]` 的语义差别值得体会：`#[ignore]` 让缺 GPU 的环境一路绿灯（可能掩盖「根本没人跑过这些测试」），显式 expect 让缺失变成可见的失败。代价是：没有 GPU 的机器跑不全量 `--lib`，必须配合过滤命令。

另外两条构建现实：

- **core 的测试编译隐含 kernels 的 nvcc 构建**。core 没有 build.rs（u1-l1），但依赖 `pegainfer-kernels`，后者的 build.rs 要编译 `csrc/*.cu`。机器上没有 `nvidia-smi` 时需设 `PEGAINFER_CUDA_SM`（如 `120`）指明 SM 目标（[CLAUDE.md:L39](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md#L39)）。
- **`--release` 是纪律**：[CLAUDE.md:L23](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md#L23) 明确 debug 构建慢到会超时，本手册所有 cargo 命令一律带 `--release`。

#### 4.3.2 核心流程

三条命令各自命中的测试集合：

```text
cargo test --release -p pegainfer-core --lib
  → src/** 里 43 个 #[test]（含 kv_pool 4 个 GPU 测试；无 GPU 机器上这 4 个失败）
  → 不含 tests/ 目录，不含 doctest

cargo test --release -p pegainfer-core --test weight_loader_gpu
  → 仅 tests/weight_loader_gpu.rs 的 2 个测试（需 GPU、不需模型权重）

cargo test --release -p pegainfer-core --lib -- --ignored
  → 0 个测试（本 crate 无 #[ignore] 标注）
```

`PEGAINFER_*` 环境变量的归属（防止张冠李戴）：

| 环境变量 | 谁读它 | 与 core 测试的关系 |
|---|---|---|
| `PEGAINFER_TEST_MODEL_PATH` | 模型 crate 的精度/集成测试（[CLAUDE.md:L44](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md#L44)，默认 `models/Qwen3-4B`） | 无关——core 的测试不加载模型 |
| `PEGAINFER_TRACE_OTLP_ENDPOINT` | core 自己的 tracing.rs（[tracing.rs:L33](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tracing.rs#L33)） | core 唯一读取的 `PEGAINFER_*` 变量 |
| `PEGAINFER_CUDA_SM` | kernels 的 build.rs（nvcc 目标探测） | 决定 core 测试能否在无 nvidia-smi 的机器上完成编译 |

#### 4.3.3 源码精读

- [kv_pool.rs:L452-L456](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L452-L456)：`test_pool` 辅助函数——「最小 GPU 足迹」的样板注释值得学：*Tiny allocation: 1 layer, 1 head, dim=1 — just enough to exercise logic*。GPU 测试尽量小，逻辑测试不烧显存。
- [kv_pool.rs:L458-L471](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L458-L471)：`storage_width_sets_backing_capacity` 直接调用 `DeviceContext::new().expect("GPU required for kv_pool tests")`（L460）——与 4.3.1 的结论互证：GPU 依赖靠 expect 表达，不靠 `#[ignore]`。
- [tests/weight_loader_gpu.rs:L1-L11](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/tests/weight_loader_gpu.rs#L1-L11)：文件头 `//! GPU, model-free: StagedWeightLoader state-machine contract.` 一句话讲清定位——**要 GPU、不要模型**；导入 `pegainfer_core::tensor::DeviceContext` 表明它是黑盒使用公开 API 的集成测试。
- [tests/weight_loader_gpu.rs:L12-L37](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/tests/weight_loader_gpu.rs#L12-L37)：`record_after_finish_is_rejected`——L14 `DeviceContext::new().unwrap()` 同样是「需要 GPU」的声明；测试体在内存里现场 serialize 一个 safetensors 分片（不读盘、不下模型），验证加载器状态机「finish 之后拒绝再登记」的契约。
- [weight_loader.rs:L294-L300](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/weight_loader.rs#L294-L300)：全 crate 唯一的 ```` ```ignore ````——`mmap_shards` 文档里的两行链式示例被标为 ignore，因此 rustdoc 不会把它当 doctest 运行。这与测试属性 `#[ignore]` 是两码事，但常常被混为一谈，此处一并澄清。

#### 4.3.4 代码实践：三条命令的对照实验

1. **实践目标**：用真实输出验证 4.3.2 的三行结论，特别是「`--ignored` 跑 0 个测试」。
2. **操作步骤**（在仓库根，逐条执行并记录输出）：

   ```bash
   # ① 全量 lib 单测（有 GPU 机器预期全绿；无 GPU 机器预期 kv_pool 4 项失败）
   cargo test --release -p pegainfer-core --lib 2>&1 | tail -n 20

   # ② 只触发被「想象中」忽略的测试——验证它们不存在
   cargo test --release -p pegainfer-core --lib -- --ignored

   # ③ 列出测试清单，统计 ignored 标记
   cargo test --release -p pegainfer-core --lib -- --list | grep -c ': test' || true
   ```

3. **需要观察的现象**：
   - ① 的汇总行 `test result:` 给出 passed/failed 计数；无 GPU 时失败信息里可见 `GPU required for kv_pool tests`（来自 expect 的消息）。
   - ② 每个测试目标显示 `running 0 tests`、`0 filtered out` 之类的空集结果——因为没有任何测试带 `#[ignore]`。
   - ③ 列表里的测试名后缀只有 `: test`，没有 `: ignored`（具体输出格式待本地验证）。
4. **预期结果**（基于源码统计，运行数字待本地验证）：① 中 lib 目标共 43 个单测，其中 kv_pool 的 `storage_width_sets_backing_capacity`、`kv_state_lifecycle`、`kv_state_out_of_pages`、`kv_state_drop_returns_pages` 4 项需 GPU；② 结果为 0 个测试被执行；③ ignored 计数为 0。
5. **无 GPU/无工具链时的替代（源码阅读型）**：
   - 阅读 [kv_pool.rs:L540-L548](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L540-L548)，写出两条断言的预期结果：`ensure_capacity(32).unwrap()` 成功（恰好 2 页），`ensure_capacity(33).is_err()` 为真（需第 3 页但池只剩 padding 页）。
   - 解释为什么同为 kv_pool.rs 里的测试，`stride_geometry_qwen3`（L485-L493）无 GPU 也能过而 `kv_state_lifecycle`（L495-L538）不行：前者只建纯值 `KvLayout`，后者经 `test_pool` 建立真实 GPU 缓冲（`CudaSlice<bf16>`）。

#### 4.3.5 小练习与答案

**练习 1**：在没有任何 GPU 的机器上执行 `cargo test --release -p pegainfer-core --lib`，会发生什么？是编译失败、测试失败还是全部跳过？
**答案**：分两段看。若 CUDA 工具链可用（可用 `PEGAINFER_CUDA_SM` 显式指定目标，kernels 的 nvcc 构建能完成），则编译通过、测试运行，kv_pool 的 4 个 GPU 测试在 `DeviceContext::new().expect(...)` 处 panic 失败，其余纯 CPU 测试照常通过；若连 nvcc 都不可用，则在构建 pegainfer-kernels 阶段就编译失败，一个测试都不会运行。

**练习 2**：`cargo test --release -p pegainfer-core --lib -- --ignored` 的输出里会运行几个测试？为什么？
**答案**：0 个。`--ignored` 只运行带 `#[ignore]` 属性的测试，而本 crate 没有任何测试带该属性（全 crate grep 仅命中 `eq_ignore_ascii_case` 与一处 ```` ```ignore ```` 文档示例）。

**练习 3**：`PEGAINFER_TEST_MODEL_PATH=models/Qwen3.5-4B cargo test ...`（CLAUDE.md L69 的写法）对 core 的测试有效吗？
**答案**：无效。该变量只被模型 crate 的精度/集成测试（如 qwen35 的 `hf_golden_gate`、`e2e_scheduler`）读取；core 的测试不加载模型权重，其唯一读取的 `PEGAINFER_*` 变量是 `PEGAINFER_TRACE_OTLP_ENDPOINT`（tracing.rs L33）。

## 5. 综合实践：给你的机器出一份「测试体检报告」

任务：把本讲三条线索（依赖 → 测试分布 → GPU 边界）合成一张可长期维护的对账表。

1. **建表**：三列——`测试名 | 所在文件 | GPU 需求（纯 CPU / 需 GPU / 待确认）`。
2. **填表（源码侧）**：从 [page_pool.rs:L132-L193](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L132-L193) 和 [kv_pool.rs:L448-L562](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L448-L562) 开始，逐个登记 9 个池类测试的 GPU 需求判断依据（「构造了什么」比「测试名像什么」可靠）。
3. **对账（运行侧，需本地环境）**：运行 4.3.4 的三条命令，把实际 passed/failed 数与表中对「需 GPU」的标注互相印证；无 GPU 机器上，失败清单应当**恰好**是表中标注「需 GPU」的那些。
4. **扩展**：把另外 10 个含测试的文件（logging、rope、eos、decodable_vocab、cpu_topology、cuda_graph/dump、cuda_graph/dump/render、ops/call_trace、weight_loader、weight_loader/staging）按同样方法补进表里；构造方式没把握的先标「待确认」，跑完 ① 再回填。
5. **产出**：表格 + 一段结论（例：「本机无 GPU，43 个单测中 N 个失败，全部失败于 DeviceContext 初始化；`--ignored` 为 0」）。这份表就是你在 u5-l2 做贡献演练时选择「验证层级」的决策依据。

## 6. 本讲小结

- **Cargo 依赖图**：core 的 22 个运行时依赖全部 `workspace = true`（版本集中在根 Cargo.toml），唯一的 path 依赖是 `pegainfer-kernels`；`tempfile` 是唯一 dev-dependency；仅有一个非默认 feature `kernel-call-trace`；clippy pedantic 等级继承自 workspace。没有 `thiserror`——错误处理统一 `anyhow`。
- **内嵌单元测试**：43 个 `#[test]` 分布在 12 个文件的 `#[cfg(test)] mod tests` 里，编译期整体裁剪；甚至存在函数级 `#[cfg(test)]`（`KvState::reset`、`KvDesc::num_pages`）让生产二进制不含测试专用方法。
- **纯 CPU 与需 GPU 的分界**：判据是「构造了什么」——`KvLayout` 是纯值类型所以 stride 测试免 GPU；`KvPool`/`KvState` 持 `CudaSlice<bf16>` 所以生命周期测试必须建 `DeviceContext`。
- **GPU 测试隔离的真实约定**：没有 `#[ignore]`；隔离靠三层——CLAUDE.md 的命令分层（`--lib` 快车道 vs 按名跑 GPU 测试）、Cargo 目标分层（`tests/weight_loader_gpu.rs` 不被 `--lib` 触发）、运行时 `DeviceContext::new().expect("GPU required ...")` 显式红灯。
- **环境变量归属**：`PEGAINFER_TEST_MODEL_PATH` 属于模型 crate 测试，`PEGAINFER_TRACE_OTLP_ENDPOINT` 才是 core 自己读的，`PEGAINFER_CUDA_SM` 决定 kernels 能否在无 nvidia-smi 的机器上完成 nvcc 编译（core 测试的编译前置）。

## 7. 下一步学习建议

本讲解决了「怎么跑」，下一讲进入「跑的是什么」：**u2-l1（tensor.rs：动态 GPU 张量与 DeviceContext）** 将拆开本讲反复出现的 `DeviceContext::new()` 与 `CudaSlice`，讲清 GPU 上下文、流与张量分配。若你想先把状态层吃透，也可跳到 **u3-l1（page_pool.rs）**——本讲 4.2 已经把它的三个测试读了一半，RAII permit、absorb 与 try_grow 的完整语义正好接着讲。阅读源码时建议带着本讲的问题清单：每遇到一个构造函数，先问「它碰 GPU 吗？」——这个问题在本 crate 几乎等价于「它能在 CI 的快车道上被测到吗？」。
