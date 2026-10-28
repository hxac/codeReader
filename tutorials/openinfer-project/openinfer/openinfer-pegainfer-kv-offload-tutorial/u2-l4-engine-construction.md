# u2-l4 引擎构造与注册：new、with_arenas_on 与 register

## 1. 本讲目标

学完本讲，你应该能够：

1. **对比两条构造路径**：`OffloadEngine::new`（私有 host + 融合 `KvBuffer`）与 `OffloadEngine::with_arenas_on`（共享 host + 显式 `KvArena` 列表）各自的适用场景、参数面与调用方义务。
2. **逐个说清 `register_context_layer_batch_strided` 的 17 个参数**里 `layer_names` / `data_ptrs` / `block_stride_bytes` / `page_first` 的确切含义——它们是 pegainfer 布局与 pegaflow 注册表之间的全部接触面。
3. **解释 `PEGAINFER_KV_TRANSFER_MODE`** 在 Direct 与 Kernel 两种传输后端之间的取舍依据：为什么默认 Direct、什么部署该翻成 Kernel。
4. **论证指针稳定性契约**：为什么 `register` 把裸设备地址「烤」进注册表之后，arena 的设备分配必须终身保持指针稳定。

本讲只覆盖 `engine.rs` 的**构造半边**。save / query / load / flush 四条链路是后面四讲（u2-l5 ～ u2-l8）的内容；本讲结束时你只会在 `register` 的末尾看到 `write_barrier` 被初始化成空状态，这是正常的。

## 2. 前置知识

本讲站在 u2-l1（配置层）、u2-l2（布局映射）、u2-l3（共享宿主）三讲的肩膀上。开始前请确认你还记得：

- **`KvArena` 五字段**（u2-l2）：`name`（引擎内唯一）、`base_ptr`（裸设备基址）、`num_blocks`、`bytes_per_block`（每块拷贝单元字节）、`block_stride_bytes`（相邻块步长，可大于拷贝单元）。
- **`Registration` 八向量**（u2-l2）：pegaflow 的批量注册 API 只收逐层向量，`from_arenas` 逐 arena 填充，并断言 \( \text{bytes\_per\_block} \le \text{block\_stride\_bytes} \)，`size_bytes` 按「最后一块到达界」\( (\text{num\_blocks}-1)\times\text{stride}+\text{bytes\_per\_block} \) 计算。
- **`OffloadHost` 共享模型**（u2-l3）：一个 host 持有一个 `PegaEngine` + 一个多线程 tokio runtime，可服务任意多个 rank 级引擎；块按 `(namespace, hash)` 寻址，namespace 相同即可跨 rank 命中。

再补几个本讲新出现（或需要更精确化）的术语：

| 术语 | 解释 |
|------|------|
| **裸设备地址（raw device address）** | 一个 `u64`，是 CUDA 设备显存里的字节偏移。它没有类型、没有生命周期、不指向任何 Rust 对象——编译器无法帮你检查它是否还有效 |
| **pegaflow 实例（instance）** | pegaflow 侧一次注册的单位，由 `(instance_id, namespace, device_id, tp_rank, pp_rank, …)` 定位。一个 host 上可以挂多个实例 |
| **DP（Data Parallel）rank** | 权重完全复制的并行等级。DP 各 rank 的 KV 字节可互换（FP 归约顺序差异以内），所以能共享一个 namespace 互相恢复 |
| **DMA 引擎（copy engine）** | GPU 上独立于流式多处理器（SM）的专用拷贝单元。`cuMemcpyAsync` 走 DMA 引擎，不占用 SM；自写的 grid-strided 拷贝 kernel 跑在 SM 上 |
| **CUDA context** | 设备的运行时上下文。pegaflow 附着设备的 primary context——与 pegainfer 相同的 context——所以两边注册/使用的指针互相有效 |
| **「烤进」（bake in）** | 构造时把地址写死进某个表，之后不再更新。本讲的核心风险来源 |

一个直白的比喻先立起来：**`register` 像是把一份「房间平面图 + 门牌号对照表」交给搬运公司（pegaflow），之后每次搬运（save/load）搬运公司只看这份表找房间的货架，不再问房东（pegainfer）**。平面图画错、或者房间被拆迁重盖而表没换，搬运工就会搬走别人房间里的东西——这就是本讲反复强调的两件事：几何要对（u2-l2）、地址不能变（本讲 4.3）。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [src/engine.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L1-L505) | 本讲主战场：`new` / `with_arenas_on` / 私有 `register`，以及拓扑常量与 `OffloadEngine` 结构体 |
| [src/layout.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs#L1-L113) | `KvArena` 与 `Registration`：两条构造路径共用的几何翻译层（u2-l2 已精读，本讲只取用） |
| [src/host.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L1-L156) | `OffloadHost`：`new` 路径在引擎构造内部私建它，`with_arenas_on` 路径接收别人建好的它 |
| [src/config.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/config.rs#L1-L99) | `OffloadConfig` 与 `host()` 拆分：`new` 路径的参数来源 |
| [../pegainfer-qwen3/src/executor.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2303-L2364) | 下游真实调用方：`build_offload` 用 `new` 路径起引擎，并演示 namespace 的推导 |
| [../pegainfer-kv-store/src/tier.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-store/src/tier.rs#L172-L217) | 对照组：兄弟 crate 直接调同一个 pegaflow 注册 API，展示「多设备注册」与「名字集合校验」的变体 |
| [../pegainfer-kv-cache/src/buffer.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L63-L66) | `KvBuffer::device_ptr`：`new` 路径读取裸基址的地方 |
| [tests/cpu_roundtrip.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/tests/cpu_roundtrip.rs#L99) | `new` 的第二个调用方（集成测试，需 GPU） |

## 4. 核心概念与源码讲解

### 4.1 私有宿主路径：OffloadEngine::new（fused KvBuffer）

#### 4.1.1 概念说明

`OffloadEngine::new` 是「**一个引擎独占一个 host**」的便捷路径：调用方只交出 `OffloadConfig` + 一个融合布局的 `KvBuffer`，函数内部自建私有 `OffloadHost`，再把 buffer 翻译成注册表喂给 pegaflow。它解决的问题是——**最常见情形（单卡单 rank 的 dense 模型，如 qwen3）下，调用方不该需要理解 host / arena / 注册表三层**，一个构造函数全包。

这条路径的三个隐含决定：

1. **host 归引擎私有**（`Arc<OffloadHost>` 只被这个引擎持有），引擎 drop → 末句柄 drop → runtime 关闭（u2-l3 的 drop 语义）。
2. **布局只能来自融合 buffer**：几何翻译固定走 `Registration::from_buffer`——page-first 融合布局 → 每个模型层一个 arena，步长等于页步长。
3. **`page_first` 硬编码为 `false`**：融合 buffer 是 pegainfer 原生的 layer-first 布局，本路径没有「加入别人 namespace」的需求，所以连这个参数都不暴露。

#### 4.1.2 核心流程

```
OffloadEngine::new(config, buffer, stream)
│
├─ 1. reg ← Registration::from_buffer(buffer, stream)
│        # u2-l2 的几何翻译：每模型层一个 KvArena
│        #   base_ptr = buffer 基址 + layer × layer_bytes
│        #   bytes_per_block = layer_stride × 2（bf16）
│        #   block_stride_bytes = page_stride × 2
│
├─ 2. host ← OffloadHost::new(config.host())?
│        # u2-l3 的宿主构造：runtime + PegaEngine（须在 runtime.enter() 内）
│        # config.host() 只带走宿主四字段（池容量/hugepages/线程/p2p）
│
└─ 3. engine ← Self::register(host, config.instance_id, &config.namespace,
                              config.device_id, reg, page_first = false)
         # 公共收口：真正打给 pegaflow（见 4.3）
```

注意身份三字段（`instance_id` / `namespace` / `device_id`）**不进** host，而是在第 3 步直接交给 `register`——这是 u2-l1 讲过的「公私拆分」：宿主字段归宿主，身份字段归实例。

#### 4.1.3 源码精读

先看函数与它的文档契约：

> [engine.rs:L94-L116](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L94-L116)：`new` 的完整实现。文档注释约定 `stream` 必须是拥有 `buffer` 的流——但**只用来读基址**；pegaflow 为自己的 worker 传输附着设备的 primary CUDA context（与 pegainfer 同一个 context），所以注册进去的指针在两边都有效。

```rust
pub fn new(
    config: OffloadConfig,
    buffer: &KvBuffer,
    stream: &CudaStream,
) -> Result<Self, EngineError> {
    let reg = Registration::from_buffer(buffer, stream);
    let host = OffloadHost::new(config.host())?;
    Self::register(
        host,
        config.instance_id,
        &config.namespace,
        config.device_id,
        reg,
        false,              // ← page_first 硬编码：原生 layer-first
    )
}
```

`stream` 的实际用途只有一处：

> [buffer.rs:L63-L66](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L63-L66)：`KvBuffer::device_ptr` 拿着 stream 锁返回裸 `u64` 基址。这就是注册进 pegaflow 的那个数——之后 pegaflow 自己开流做 D2H/H2D，不再用 pegainfer 的流。

`config.host()` 的拆分在 u2-l1 已读，这里只提醒它带走了什么：

> [config.rs:L64-L71](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/config.rs#L64-L71)：`host()` 只携带 `pinned_pool_bytes` / `use_hugepages` / `runtime_threads` / `p2p` 四个宿主字段；`instance_id` / `namespace` / `device_id` 留给 `register`。

宿主构造的细节（runtime.enter() 守卫、P2P 三道 fail-fast、GC）是 u2-l3 的内容；本路径只是把 u2-l3 的 `OffloadHost::new` 当库函数调用：

> [host.rs:L40-L67](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L40-L67)：`OffloadHost::new` 建多线程 runtime，把 p2p 字段注入 `StorageConfig`，并在 `runtime.enter()` 守卫内构建 `PegaEngine`（否则其内部 `tokio::spawn` 直接 panic）。

仓库内 `new` 的两个真实调用方：

> [executor.rs:L2356-L2357](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2356-L2357)：qwen3 的 `build_offload` 用 `OffloadEngine::new(config, kv_mgr.buffer(), &ctx.stream)` 起引擎——生产路径。

> [cpu_roundtrip.rs:L99](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/tests/cpu_roundtrip.rs#L99)：本 crate 的 GPU roundtrip 集成测试同样走 `new`——测试与生产共用同一条构造路径，测试覆盖的就是生产几何。

qwen3 调用现场还值得看一眼的是 namespace 怎么来的（这是「身份字段」的下游语义）：

> [executor.rs:L2318-L2341](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2318-L2341)：namespace 由模型规格（hidden_size / intermediate_size / vocab_size）**加上** KV 几何（层数/头数/头维/页大小）拼成。注释解释了为什么光有几何不够：Qwen3-4B 与 8B 的 KV 几何完全相同，只编几何会让混合 mesh 静默串块。vLLM 兼容模式则改用操作者显式传入的 P 侧 namespace。

#### 4.1.4 代码实践

**实践目标**：不动一行代码，用检索工具把 `new` 的调用链与调用方摸清，并验证「身份字段不进 host」这条拆分。

**操作步骤**：

1. 在仓库根目录运行：
   ```bash
   grep -rn "OffloadEngine::new" --include="*.rs" .
   ```
   预期只有两处命中：qwen3 的 executor.rs 与本 crate 的 cpu_roundtrip.rs（`with_arenas_on` 会在 4.2 验证：除定义外零命中）。
2. 打开 [executor.rs:L2303-L2364](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2303-L2364)，读 `build_offload` 的文档注释，注意它要求「必须在 stream 移交 `RankWorker` 之前调用」——想一想这与 `new` 只用 stream 读基址是否矛盾（答案见小练习 1）。
3. 读 [executor.rs:L2342-L2355](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2342-L2355)：对照 u2-l1，标出哪些 `OffloadConfig` 字段经构造参数、哪些经公开字段赋值、哪些经 builder。

**需要观察的现象**：`build_offload` 里没有任何一行直接触碰 `OffloadHost` / `KvArena` / `register_context_layer_batch_strided`——host 与几何全部被 `new` 吞掉。

**预期结果**：你能画出新路径的完整时序：`build_offload` → `OffloadConfig` 组装（namespace 推导）→ `new` → `from_buffer` + `OffloadHost::new` → `register`。全程调用方只见 `config + buffer + stream` 三个参数。

（纯阅读实践，无需 GPU，无需编译。）

#### 4.1.5 小练习与答案

**练习 1**：`new` 的文档说 `stream` 「只用来读基址」，qwen3 的 `build_offload` 却要求「stream 还没移交 `RankWorker` 时调用」。这两者矛盾吗？

<details><summary>参考答案</summary>

不矛盾，但后者比前者更保守。「只读基址」是**本函数**的最低需求：`device_ptr(stream)` 需要拿着 stream 的锁访问 buffer。qwen3 的要求来自**时序安全**：在 stream 归属还没转移、KV 池还没开始跑请求时构造引擎，保证注册发生在任何 KV 写入之前——注册与首次 save 之间不会出现「地址还没注册、块已经在被写」的窗口。这是调用方侧的防御性排序，不是 `new` 的硬性要求。
</details>

**练习 2**：为什么 `new` 不暴露 `page_first` 参数？

<details><summary>参考答案</summary>

`page_first = true` 只在**加入一个由别人写、按 page-first 存储的 namespace** 时才有意义（见 4.2 的 vLLM MLA 场景）。`new` 路径的输入是 pegainfer 自己的融合 `KvBuffer`——原生 layer-first 布局，注册语义唯一确定，所以硬编码 `false`。把一个恒定的值暴露成参数只会制造「传错了会怎样」的疑问面；真需要翻转时走 `with_arenas_on`，那里的 arena 与 `page_first` 一起由调用方负责。
</details>

**练习 3**：`OffloadConfig` 的七个字段里，哪些进 `host()`、哪些进 `register`？

<details><summary>参考答案</summary>

进 `host()`（宿主四字段）：`pinned_pool_bytes`、`use_hugepages`、`runtime_threads`、`p2p`。进 `register`（身份三字段）：`instance_id`、`namespace`、`device_id`。判据是「这份配置描述的是共享池与运力，还是本实例的身份与寻址域」——host 可被多实例共享所以不能含身份；instance 是注册单位所以身份必须随注册走。
</details>

### 4.2 共享宿主路径：with_arenas_on（显式 arena 列表）

#### 4.2.1 概念说明

`with_arenas_on` 是「**多个引擎挂一个既有 host**」的路径：调用方先建好（或拿到）一个 `Arc<OffloadHost>`，再为每个 rank 用显式的 `KvArena` 列表注册成 host 上的**又一个 pegaflow 实例**。它是 DP-rank 共享模型与跨引擎 namespace 的入口，也是 sidecar 模型（GLM5.2 式「一模型层多个 arena」）进入注册表的唯一公共通道。

它与 `new` 的本质差异不在「参数多了几个」，而在**责任方向反转**：

- `new`：crate 替你建 host、替你算几何——便利换自由度。
- `with_arenas_on`：你自备 host、自报几何——自由度换契约。几何不再是「从 `KvBuffer` 推导出的真理」，而是「调用方的声明」，于是三条契约全部写在文档注释里由调用方担保（见 4.2.3）。

诚实的现状说明：**`with_arenas_on` 当前在仓库内没有调用方**（只有定义处命中）。它是为两类场景预留的公共入口：同机 DP-rank 共享一个 host/pinned 池、以及加入外部 writer（vLLM connector）的 MLA namespace。它的全部语义由文档注释承载——这也使它成为「文档注释即契约」的教科书样本。

#### 4.2.2 核心流程

```
with_arenas_on(host(共享), instance_id, namespace, device_id, arenas, page_first)
│
├─ 1. reg ← Registration::from_arenas(arenas)
│        # 不查任何 KvBuffer：arena 几何是调用方的声明
│        # 逐 arena 断言 bytes_per_block ≤ block_stride_bytes
│        # size_bytes = (num_blocks−1)×stride + bytes_per_block
│
└─ 2. engine ← Self::register(host, instance_id, namespace, device_id,
                              reg, page_first)      # ← 唯一来源：调用方
         # host 不新建，只 Arc 共享 → host 生命周期由所有挂靠引擎共同决定
```

关键点：`page_first` 在这条路径上是**参数**而不是常量，因为它必须匹配「namespace 的 writer 怎么存块」——这是加入别人内容域时的对端约定，本 crate 无法替你决定。

#### 4.2.3 源码精读

> [engine.rs:L118-L146](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L118-L146)：`with_arenas_on` 的完整签名与文档注释。文档讲了三件事：这个 rank 成为 host 单一池上的「又一个 pegaflow 实例」；想互相看到块的 rank 传相同 namespace；arena 的设备分配必须终身活跃且指针稳定。

```rust
pub fn with_arenas_on(
    host: Arc<OffloadHost>,
    instance_id: impl Into<String>,
    namespace: &str,
    device_id: i32,
    arenas: &[KvArena],
    page_first: bool,
) -> Result<Self, EngineError> {
    Self::register(
        host,
        instance_id.into(),
        namespace,
        device_id,
        Registration::from_arenas(arenas),
        page_first,
    )
}
```

文档注释里的三条调用方契约（原文在 [engine.rs:L122-L129](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L122-L129)）：

1. **指针稳定契约**：每个 arena 的设备分配必须终身保持活跃且指针稳定——注册会把裸设备地址烤进去（4.3 详述）。
2. **同池块号契约**：所有 arena 必须由**同一套池块号**索引。pegaflow 对同块号的所有 arena 一起搬运，sidecar 才能与主 KV 同步（u2-l2）。
3. **`page_first` 对齐契约**：必须与 namespace 的 writer 的存储方式一致。vLLM connector 把 MLA 模型的块按 page-first 存（一个块的所有层拼进一个 host 页，按**层名字典序**偏移），所以加入 vLLM MLA namespace 必须 `true`，且层名与每层块字节数和 writer 完全一致。

`from_arenas` 这半边在 u2-l2 已逐行精读，本讲只需要它作为「声明式几何」的证据：

> [layout.rs:L81-L112](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs#L81-L112)：`from_arenas` 把每个 arena 平铺进八个向量，断言拷贝单元不越步长，`size_bytes` 取最后一块到达界。注意它**不接触任何 CUDA 对象**——`base_ptr` 是调用方给的 `u64`，正确性完全靠上述契约。

共享语义的根据在 host 侧文档：

> [host.rs:L15-L29](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L15-L29)：「一个 host 服务任意多个 rank 级 `OffloadEngine`」——每个 rank 把自己的 GPU arena 注册成自己的 pegaflow 实例，但块都落进同一个 host 层、以 `(namespace, hash)` 为键。共享 namespace 的前提是各实例的 KV 可互换（replicated-weight DP 满足，容差等同复用本 rank 前缀缓存）。

「一个 host 挂多实例」并非本 crate 的发明，而是 pegaflow 注册模型的原生能力。兄弟 crate `pegainfer-kv-store` 就在更复杂的形态下直接调同一个 API：

> [tier.rs:L172-L217](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-store/src/tier.rs#L172-L217)：`pegainfer-kv-store` 把主设备 + 多个 mirror 设备逐个注册成同一实例下的多台设备，`world_size = 1 + mirrors.len()`（对比本 crate 的 `WORLD_SIZE = 1` 常量），并且自己维护逐层向量。

它还展示了一条本 crate 没有的防御，很能说明这类注册的坑：

> [tier.rs:L150-L170](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-store/src/tier.rs#L150-L170)：mirror 的 arena 名字集合不一致**不会让注册失败**——pegaflow 会把不相交的名字集合当成 layer SPLIT，把 load 静默路由到恰好一台设备。所以 kv-store 在边界上显式校验名字与几何全等。教训：批量注册 API 对「错误的名字集合」是宽容的，宽容即静默。

#### 4.2.4 代码实践

**实践目标**：产出本讲对比表的第一版（前三个维度），并用检索证实「`with_arenas_on` 仓库内无调用方」这一事实。

**操作步骤**：

1. 运行：
   ```bash
   grep -rn "with_arenas_on" --include="*.rs" .
   ```
   预期只命中 `src/engine.rs` 的定义处（以及教程文档）。
2. 通读 [engine.rs:L118-L129](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L118-L129) 的文档注释，用自己的话把三条契约各写成一句话（中文，不含「契约」二字本身）。
3. 起草对比表，维度先填三个：**host 归属**（谁建、谁持有、何时死）、**布局来源**（从哪推出几何、谁是真理来源）、**page_first 取值**（常量还是参数、为什么）。「典型调用方」维度留到 4.3 之后补全。

**需要观察的现象**：文档注释对「什么时候该用这条路径」给出的是场景（DP-rank 共享、vLLM MLA namespace）而不是命令行或配置项——公共 API 的可发现性靠类型签名 + 文档，而不是靠 wiring。

**预期结果**：一张三行对比表草稿 + 三句契约改写。参考答案会在第 5 节综合实践给出完整版。

（纯阅读实践，无需 GPU。）

#### 4.2.5 小练习与答案

**练习 1**：同一台机器上两个 DP rank 想互相恢复对方 save 的块，最少要满足哪些条件？

<details><summary>参考答案</summary>

三个：(1) 挂**同一个** `OffloadHost`（各自 `OffloadHost::new` 建出的两个池互不相见）；(2) `with_arenas_on` 传**相同的 namespace**；(3) KV 字节事实上可互换——replicated-weight DP 满足（各 rank 归约顺序差异在「本 rank 重算同一前缀也会有的差异」量级内）。前两条是机制，第三条是语义，机制无法替你担保第三条。
</details>

**练习 2**：`with_arenas_on` 的 `arenas` 参数与 `new` 的 `buffer` 参数，谁的「真理来源」更强？分别靠什么保证正确？

<details><summary>参考答案</summary>

`new` 的 `buffer` 更强：`KvBuffer` 是活的 CUDA 分配，`from_buffer` 直接向它查询 `layout()` / `num_blocks()` / `device_ptr()`，几何错误会在源头暴露。`with_arenas_on` 的 `arenas` 只是声明——`from_arenas` 只能断言内部自洽（拷贝不越步长），无法验证 `base_ptr` 真的有效、`num_blocks` 真的有那么多块。所以正确性从「类型保证」降级为「文档契约保证」，这正是它灵活性（任意 sidecar 布局、外部 namespace）的代价。
</details>

**练习 3**：为什么「同池块号索引」是 sidecar 正确性的关键，而不是一个可有可无的约定？

<details><summary>参考答案</summary>

pegaflow 的搬运单位是「(层名, 块号)」集合：save/load 对**同一块号下注册的所有 arena** 一起动手。sidecar（如 GLM5.2 的 index-K）与主 KV 若共享块号，则任何一次 save/load 都天然同步搬两边——这是「免费的一致性」。若 sidecar 用了自己的编号空间，块号对不上，一致性立刻退化为「两条独立搬运链」，必然漂移。
</details>

### 4.3 register：一次批量注册的 17 个参数、page_first 与 TransferMode

#### 4.3.1 概念说明

两条构造路径在私有函数 `register` 收口：它把 `Registration` 的八个向量摊开成 `register_context_layer_batch_strided` 的 17 个参数，一次调用完成「pegainfer 侧几何 → pegaflow 侧注册表」的全部翻译。注册之后，**本 crate 与 pegaflow 之间再无几何信息交换**——save 只说「(层名列表, 块号, hash)」，load 只说「(租约, 目标块号)」，一切地址由 pegaflow 按注册表还原。

这就是「烤进」的含义：注册表是**一次性快照**。它带来两个必须理解清楚的结果：

- **地址寻址公式化**。某层某块的设备地址 = \[ \text{base\_ptr}_{\text{layer}} + \text{block\_id} \times \text{block\_stride\_bytes}_{\text{layer}} \]，拷贝 \(\text{bytes\_per\_block}_{\text{layer}}\) 字节。融合布局下 \(\text{block\_stride\_bytes} = \text{page\_stride} \times 2\)（步长大于拷贝），sidecar 下步长等于拷贝。
- **指针稳定性成为硬约束**。`data_ptrs` 是裸 `u64`，注册后永不刷新。设备分配若被释放/搬移而注册表没换，后续 D2H/H2D 读到的是「新住户」的字节——u1-l2 讲过，这是唯一致命的错误类别（错数据以旧 hash 持久化 = 静默腐蚀）；或者干脆访问非法地址崩溃。所以：`new` 路径下 `KvBuffer` 的分配必须活得比引擎久（pegainfer 的 KV 池启动时一次分配、终生不动）；`with_arenas_on` 把这个义务显式写进文档交给调用方。这与 CLAUDE.md 里 CUDA Graph「预分配缓冲保指针稳定」是同一条纪律。

`TransferMode` 是注册时同时敲定的**传输后端**选择——拷贝用 DMA 引擎（Direct）还是用自写 kernel（Kernel）跑 SM。它看似实现细节，实则是「保护谁的计算」的部署决策（见 4.3.3）。

#### 4.3.2 核心流程

`register` 的 17 个参数按语义分五组：

| 组 | 参数 | 取值来源 | 含义 |
|----|------|---------|------|
| **身份** | `instance_id` | 调用方 | 本实例在 pegaflow 的稳定标识（跨请求可见性的锚） |
| | `namespace` | 调用方 | 内容寻址域：两引擎互通 ⇔ namespace 相同 |
| | `device_id` | 调用方 | CUDA 设备序号 |
| **拓扑** | `tp_rank` / `pp_rank` / `tp_size` / `world_size` | 常量 0/0/1/1 | 本 crate 只写单卡单 rank；kv-store 的对照实现会算真 world_size |
| **逐层几何** | `layer_names` | `reg.layer_names` | pegaflow 侧的层身份；save/load 按名字扇出到每层 |
| | `data_ptrs` | `reg.data_ptrs` | 每层 arena 的**裸设备基址**（被烤进注册表） |
| | `size_bytes` | `reg.size_bytes` | 每层 arena 的管辖范围（最后一块到达界），pegaflow 据此校验拷贝越界 |
| | `num_blocks` / `bytes_per_block` | `reg.*` | 块数与每块拷贝单元字节 |
| | `kv_stride_bytes` / `segments` | 固定 `0` / `1` | 单段 arena；K/V 拆段（segments=2）只属于 vLLM 式对称布局，本 crate 不用 |
| | `block_stride_bytes` | `reg.block_stride_bytes`（包在 `Some` 里） | 相邻块步长 ≠ 拷贝大小——让 page-first 融合布局能被注册的解耦参数 |
| **传输策略** | `TransferMode` | 环境变量 | Direct（DMA 引擎 cuMemcpyAsync）或 Kernel（SM 上 grid-strided 拷贝核） |
| **存储格式** | `page_first` | `new`→false / `with_arenas_on`→参数 | host 侧一个块怎么存：每层一列（layer-first）还是所有层拼进一页（page-first） |

执行流程一句话：**查环境变量定 TransferMode → 一次 `register_context_layer_batch_strided` 调用 → 用返回的注册成功装配 `OffloadEngine` 结构体**（`layer_names` 从 `reg` 移走复用为引擎字段，`write_barrier` 初始化为空）。

#### 4.3.3 源码精读

**注册调用本体**：

> [engine.rs:L156-L171](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L156-L171)：`register` 把八个向量原样摊给 pegaflow。`block_stride_bytes` 以 `Some(...)` 传入——它是这个批量注册 API 的可选参数，`None` 表示历史上的「步长 == 拷贝大小」dense ABI；融合布局步长大于拷贝，必须显式给出。拓扑四元组来自文件顶部的常量。

```rust
host.engine.register_context_layer_batch_strided(
    &instance_id, namespace, device_id,
    TP_RANK, PP_RANK, TP_SIZE, WORLD_SIZE,      // 单卡单 rank 写死
    &reg.layer_names,
    &reg.data_ptrs,                             // ← 裸地址，烤进注册表
    &reg.size_bytes,
    &reg.num_blocks, &reg.bytes_per_block,
    &reg.kv_stride_bytes, &reg.segments,        // 固定 0 / 1（单段）
    Some(reg.block_stride_bytes.as_slice()),
    /* transfer_mode */ /* page_first */,
)?;
```

> [engine.rs:L37-L42](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L37-L42)：`TP_RANK = 0`、`PP_RANK = 0`、`TP_SIZE = 1`、`WORLD_SIZE = 1`——文档注明「单 GPU 单 rank 拓扑；dense Qwen3 路径每个 executor rank 一个 offload 引擎，各拥有一块 GPU 的 KV buffer」。多设备注册（如 kv-store 的 mirror 链）不在本 crate 的 v1 范围内。

`kv_stride_bytes = 0`、`segments = 1` 的出处（即「K/V 拆段不适用于本 crate」的证据）：

> [layout.rs:L89-L90](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/layout.rs#L89-L90)：`from_arenas` 把这两项固定初始化为全 0 / 全 1——一个 arena 就是每块一个拷贝单元，K/V 在页内本就连续（u2-l2），拆段只属于 vLLM 注册的对称布局。

**TransferMode 的取舍**——这段注释是全文件信息密度最高的一段：

> [engine.rs:L172-L183](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L172-L183)：默认 Direct（DMA 引擎上的 cuMemcpyAsync）。Kernel 后端曾为「碎片化批量恢复」做过 A/B（#704），在与 decode 共存时**测得更差**——它的 grid-strided 拷贝核在 SM 上与 decode 核抢资源。而在纯 prefill rank 上没有 decode 需要保护，Direct 的逐碎片 cuMemcpyAsync 又会在 host 恢复风暴中严重串行化——所以用环境变量按部署选择。

```rust
match std::env::var("PEGAINFER_KV_TRANSFER_MODE").as_deref() {
    Ok("kernel") => pegaflow_core::TransferMode::Kernel,
    _ => pegaflow_core::TransferMode::Direct,
}
```

读懂这段注释的三层逻辑：

1. **共存的 decode 是要保护的资源**：decode 核在 SM 上跑，Kernel 拷贝也在 SM 上跑 → 抢 SM → TPOT 变差。Direct 走 DMA 引擎，物理上与 SM 无关 → decode 不受影响。这是默认 Direct 的理由。
2. **纯 prefill rank 的反转**：没有 decode 可保护时，Direct 的短处暴露——每个碎片一次 cuMemcpyAsync 调用，恢复风暴（大量小块 H2D）下调用串行化成为瓶颈；Kernel 一次 grid-strided 启动可以吃掉整个批次。
3. **所以是部署参数而非代码常量**：同一份二进制既可能跑 P/D 分离的 D 节点（有 decode）也可能跑 P 节点（纯 prefill），环境变量让运维按下刀。

注意匹配是**精确字符串** `"kernel"`：`KERNEL`、`Kernel`、`kernel ` 都会落回 Direct——静默兜底而非报错，配置拼错不会拒绝启动。对照实现（同一份注释的另一个拷贝）在 [tier.rs:L302-L314](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-store/src/tier.rs#L302-L314)。

**page_first 的存储语义**：

> [engine.rs:L184-L190](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L184-L190)：Layer-first（false）：一个模型层 = 一个 pegaflow 层，页间交错缝隙用 `block_stride_bytes` 表达——pegainfer 原生布局。Page-first（true）：一个块在 host 侧存成一页，容纳所有层、按层名字典序排偏移——仅用于加入以该方式写块的 namespace（vLLM connector 在 MLA 模型上）。

注意 `page_first` 描述的是 **host 侧（卸载层）的存储格式**，不是 GPU 侧布局——GPU 侧永远由 arena 向量描述。同一个 GPU 布局可以以两种 host 格式落盘，能不能混取决于 namespace 里 writer 怎么存。

**注册成功后的装配**：

> [engine.rs:L193-L202](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L193-L202)：`layer_names` 从 `reg` 移入引擎字段（后续 `load` 借成 `&[&str]`），`write_barrier` 初始化为「无在途 save、无前驱屏障」。构造到此结束——`OffloadEngine` 结构体本身（[engine.rs:L70-L81](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L70-L81)）轻得只有五 个字段：host、instance_id、device_id、layer_names、write_barrier。重的东西（几何、地址）全部住在 pegaflow 的注册表里。

#### 4.3.4 代码实践

**实践目标**：把 17 个参数的分组表变成你自己的产出；并精确预测 `PEGAINFER_KV_TRANSFER_MODE` 各种取值的行为。

**操作步骤**：

1. 合上讲义，对着 [engine.rs:L156-L191](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L156-L191) 的调用现场，凭记忆把 17 个参数填进 4.3.2 那张五分组表（身份 3 / 拓扑 4 / 逐层几何 8 / 传输 1 / 格式 1）。填不出的格子就是你需要回炉的知识点。
2. 对下列每个取值，**先预测**再对照 [engine.rs:L180-L183](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L180-L183) 验证：`"kernel"` / `"KERNEL"` / `"direct"` / 未设置 / 设成空字符串。
3. （可选，验证字符串匹配语义）下面是一段**示例代码**（非项目代码），隔离了 match 语义，可在任意临时目录用 `rustc` 编译运行：
   ```rust
   // 示例代码：演示 env 匹配是精确字符串比较
   fn main() {
       for v in ["kernel", "KERNEL", "direct", ""] {
           let mode = match std::env::var("X").ok().as_deref() {
               _ => v, // 占位：真实代码里这里读的是环境变量
           };
           let chosen = match mode { "kernel" => "Kernel", _ => "Direct" };
           println!("{v:?} -> {chosen}");
       }
   }
   ```

**需要观察的现象**：除 `"kernel"` 外的一切取值（含拼错、大小写错、空串）都静默落到 Direct；没有任何日志告诉你「配置没生效」。

**预期结果**：五分组表能默写；环境变量五个取值的预测全部与代码一致（`"kernel"` → Kernel，其余全部 → Direct）。

（第 1、2 步无需任何环境；第 3 步只需 rustc，无需 GPU、无需 pegaflow。）

#### 4.3.5 小练习与答案

**练习 1**：`block_stride_bytes` 为什么要包在 `Some(...)` 里传？`None` 意味着什么？

<details><summary>参考答案</summary>

它是批量注册 API 的**可选参数**：`None` 走历史 ABI——步长等于每块拷贝大小（dense 布局，stride == copy size）。pegaflow 原本只有这一种几何，page-first 融合布局（步长 > 拷贝）与它不兼容，这正是设计文档 §5.R1 记录的 ABI 冲突——解法就是给注册加 `block_stride_bytes`（解耦「块间步长」与「每块拷贝大小」）。融合布局必须显式 `Some`，sidecar（步长==拷贝）虽然数值上等于历史 ABI，也统一走 `Some`，一个代码路径不留特例。
</details>

**练习 2**：一个纯 prefill 节点（P/D 分离的 P 侧）应该选哪个 TransferMode？为什么默认值对它不是最优？

<details><summary>参考答案</summary>

选 Kernel（`PEGAINFER_KV_TRANSFER_MODE=kernel`）。默认 Direct 的理由是「别抢 decode 的 SM」，但 P 侧没有 decode 在跑，这个保护无意义；而 P 侧的典型负载是 host 恢复风暴——D 节点大量小块 H2D 拉取，Direct 对每个碎片发一次 cuMemcpyAsync，调用串行化成为瓶颈；Kernel 一次 grid-strided 启动吃掉整个批次。默认值服务的是「decode 与传输共存」的通用情形，纯 prefill 是它的反例。
</details>

**练习 3**：假设注册后 `KvBuffer` 的底层分配被释放重分配到新地址，save 会发生什么？为什么说这是「静默」的？

<details><summary>参考答案</summary>

pegaflow 仍按旧注册表算地址：\(\text{base\_ptr} + \text{block\_id} \times \text{block\_stride\_bytes}\)，D2H 会读到**新分配里恰好落在这个地址上的字节**（如果新分配覆盖同一区域）或触发非法访问（如果没有）。前者更可怕：读到的随机字节被当作 KV、以旧块的内容 hash 持久化进 host 层——hash 没错、字节错了，下游 load 回去喂给 attention，输出悄悄劣化，无任何报错。这就是 u1-l2 的结论「丢 save 只损命中，错数据入键才致命」的机制版。防御只有一条：设备分配终身指针稳定（`new` 路径由 pegainfer 的池化分配保证，`with_arenas_on` 由文档契约保证）。
</details>

## 5. 综合实践

把 4.1/4.2 两步的草稿拼成本讲的**完整对比表**——这是本讲的正式产出。参考答案：

| 维度 | `OffloadEngine::new` | `OffloadEngine::with_arenas_on` |
|------|----------------------|--------------------------------|
| **host 归属** | 私有：引擎内部 `OffloadHost::new(config.host())` 自建，`Arc` 只被本引擎持有；引擎 drop 即末句柄 drop，runtime 关闭、在途 save 被弃、P2P 停服务 | 共享：调用方传入既有 `Arc<OffloadHost>`；host 生命周期由所有挂靠引擎共同决定，一个引擎 drop 不影响别的 rank |
| **布局来源** | 活的 `KvBuffer`：`Registration::from_buffer` 向 buffer 查询 `layout()/num_blocks()/device_ptr()`，几何是查询出的真理 | 声明的 `&[KvArena]`：`Registration::from_arenas` 只做内部自洽断言，几何正确性由调用方契约担保（指针稳定、同池块号） |
| **page_first 取值** | 硬编码 `false`（pegainfer 原生 layer-first，无外部 namespace 需求） | 参数：必须匹配 namespace 的 writer——加入 vLLM connector 的 MLA namespace 须 `true`，且层名（字典序偏移的依据）与每层块字节数同 writer 一致 |
| **典型调用方** | `pegainfer-qwen3` 的 `build_offload`（生产）与本 crate `tests/cpu_roundtrip.rs`（测试）——同一条路径，测试即生产几何 | 仓库内暂无调用方：为同机 DP-rank 共享一个 host/pinned 池、以及加入外部 vLLM MLA namespace 两类场景预留的公共入口，语义由文档注释承载 |

然后完成本讲实践的**第二问——指针稳定性论证**，参考答案：

注册把 `data_ptrs`（裸 `u64` 设备地址）烤进 pegaflow 注册表后，本 crate 与 pegaflow 之间的每次搬运都按 \[ \text{addr} = \text{base\_ptr}_{\text{layer}} + \text{block\_id} \times \text{block\_stride\_bytes}_{\text{layer}} \] 由注册表还原地址，**再也不查询** `KvBuffer`/arena 本身。于是：

1. **编译器帮不上忙**：`u64` 不是引用，没有借用检查；分配被释放也不会让注册表失效——Rust 的所有权系统在这条边界上完全失明。
2. **后果分两档**：地址上的新分配恰好覆盖同一区域 → D2H 读到新住户的字节，以旧 hash 持久化 = 静默 KV 腐蚀（唯一致命类别）；地址悬空 → 非法访问崩溃（反而「响亮」，危害更小）。
3. **因此两条路径各自落实义务**：`new` 路径里 pegainfer 的 KV 池启动时一次性分配、终身不搬（`KvBuffer` 活得比引擎久是隐含前提）；`with_arenas_on` 把「every arena's device allocation must stay live and pointer-stable for the engine's lifetime」写进文档注释，交给调用方——这是从「类型保证」降级到「契约保证」时必须补上的话。

（全部为阅读与写作实践，无需 GPU；如需验证第 1 问的 grep 结论，仅需 shell。）

## 6. 本讲小结

- **两条构造路径 = 两种责任模型**：`new` 是「crate 替你建 host、替你算几何」的便捷路径（qwen3 生产与 roundtrip 测试共用）；`with_arenas_on` 是「你自备 host、自报 arena」的共享路径（DP-rank 共享与外部 namespace 入口，仓库内暂无调用方，语义全在文档注释）。
- **`register` 是唯一接触面**：17 个参数（身份 3 / 拓扑 4 / 逐层几何 8 / 传输 1 / 格式 1）一次烤进 pegaflow 注册表；此后 save/load 只说 `(层名, 块号, hash)`，地址全部由注册表按 \(\text{base\_ptr} + \text{block\_id} \times \text{block\_stride\_bytes}\) 还原。
- **`block_stride_bytes` 是解耦参数**：步长 ≠ 拷贝大小，page-first 融合布局（stride > copy）与 sidecar（stride == copy）统一走 `Some(...)`；`None` 是 dense 历史 ABI。`kv_stride_bytes=0`/`segments=1` 固定——K/V 拆段只属于 vLLM 式对称布局。
- **`page_first` 说的是 host 侧存储格式**：false = 每模型层一列（pegainfer 原生）；true = 一个块所有层拼进一页、按层名字典序偏移（加入 vLLM MLA writer 的 namespace 时必须对齐，含层名与每层字节数）。
- **TransferMode 是「保护谁的计算」的部署决策**：默认 Direct（DMA 引擎不抢 decode 的 SM，#704 的 A/B 结论）；纯 prefill rank 的恢复风暴应翻成 Kernel（`PEGAINFER_KV_TRANSFER_MODE=kernel`，精确匹配、其余静默落 Direct）。
- **指针稳定是烤进注册的代价**：裸地址永不刷新，设备分配必须终身稳定——`new` 由池化分配隐含保证，`with_arenas_on` 由文档契约显式担保；违反的下场不是报错而是错字节入旧 hash 的静默腐蚀。

## 7. 下一步学习建议

构造完成、注册表已就位，接下来四讲沿**数据流**走完引擎的另外半边：

1. **u2-l5（可轮询句柄）**：`OffloadHandle<T>` 的 poll/wait/settle 三种收账方式——后续 save/load/query 返回的所有句柄都建在它上面，先读它再读链路事半功倍。
2. **u2-l6（save 链路）**：`build_saves` 的跨层扇出（本讲 4.3 见过的 `layer_names` 在那里被逐层 clone 进 `LayerSave`）、ORDERING 与 REUSE 两条契约——本讲的「块封后才可存」直觉在那里变成正式协议。
3. **u2-l8（flush 屏障）**：`write_barrier` 字段（本讲初始化为空的那个）的真正戏份。
4. 想看「多设备注册」与「名字集合校验」的成熟变体，直接读 [pegainfer-kv-store/src/tier.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-store/src/tier.rs#L150-L217)——同一 pegaflow API 的另一个消费者，能反向加深对本 crate 取舍（单卡、无 mirror、无拆段）的理解。
