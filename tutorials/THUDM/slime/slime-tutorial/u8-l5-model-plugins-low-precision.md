# 模型插件与低精度（fp8 / int4）

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `slime_plugins/models/` 这个「模型插件包」是如何组织的，以及它与 `--spec`、`--custom-model-provider-path` 两个挂载点的关系，并能区分它与 `slime/backends/megatron_utils/model_provider.py` 内置工厂的分工。
- 理解「bf16 训练 + 低精度推理」的完整图景：训练侧永远在高精度下更新权重，推理侧（SGLang）却可能要求 fp8 / int4 权重，这一落差由谁、在哪一步抹平。
- 区分两条量化路径：**离线工具**（`tools/convert_hf_to_fp8.py` 等，在训练前把 HF 检查点一次性量化）与**在线量化**（每轮 `update_weights` 时在 trainer 上即时把 bf16 转成 fp8/int4 再下发）。
- 精读 `quantizer_fp8.py` 处理器：它如何按参数名筛选要量化的 Linear 权重、如何选择 DeepGEMM ue8m0 路径或自带 Triton 内核，以及它与 int4（`compressed-tensors`）路径在处理时机上的关键差别。

本讲承接 [u4-l5 模型构建、并行初始化与参数冻结](u4-l5-model-provider-and-init.md)：u4-l5 讲透了 `get_model_provider_func` 的内部实现与 `--spec` / `--custom-model-provider-path` / `--custom-megatron-init-path` 三个入口的分工。本讲不再重复工厂函数内部，而是聚焦两件 u4-l5 没展开的事：**第一方模型插件包长什么样、装在哪**，以及**低精度权重在训练-推理闭环里如何流转**。同时本讲也承接 [u5-l1 权重同步全景](u5-l1-weight-sync-overview.md)：低精度量化正是嵌在那条「枚举 → 聚合 → 转换」流水线的「转换」环节里。

## 2. 前置知识

在进入源码前，先补齐三组概念。

**为什么训练用高精度、推理用低精度。** 混合精度训练里，反向传播需要梯度，而梯度的数值范围远比前向权重更敏感——fp8 / int4 这类低精度格式没有可用的梯度通路（它们是「定点/缩放整数」表达，专为前向 GEMM 设计）。因此训练侧（Megatron）始终在 bf16 甚至 fp32 下维护权重与优化器状态，以保证更新方向正确；而推理侧（SGLang）为了省显存、提吞吐，会把权重压成 fp8（e4m3）或 int4。结论是：**低精度只发生在推理端，训练端永远是高精度**，二者之间的「翻译」必须每轮同步时现做。

**fp8（e4m3fn）与缩放量化。** `torch.float8_e4m3fn` 是 1 位符号、4 位指数、3 位尾数、无 NaN（fn = finite）的 8 位浮点格式，可表示范围 \([-448, +448]\)。把一个 bf16 权重张量 \(w\) 压成 fp8 的标准做法是**缩放量化**：先算一个标量（或逐块）缩放因子 \(s\)，再把 \(w/s\) 截断到 fp8 范围。最朴素的 per-tensor 写法：

\[
s = \frac{\max(|w|)}{448}, \qquad q = \mathrm{round}\!\left(\mathrm{clamp}\!\left(\frac{w}{s},\,-448,\,+448\right)\right)
\]

块级（block-wise）量化把张量切成 \((B_M, B_N)\) 小块，每块各算一个 \(s\)，精度更高：

\[
s_{ij} = \frac{\max_{(m,n)\in \mathrm{block}_{ij}} |w_{mn}|}{448}
\]

反量化时只需 \(w \approx q \cdot s\)。块越小、缩放因子越细，量化误差越小。`weight_scale_inv`（块级）与 `weight_scale`（per-tensor/逐通道）就是这些 \(s\)，与 fp8 权重一起存。

**int4 与「打包」。** 4 位整数（int4）一个值只占 4 bit，8 个 int4 可以塞进一个 int32。把权重压成 int4 后必须**打包（pack）**成 int32 才能被 AWQ/Marlin 这类内核读取，还要额外存 `weight_scale`（缩放）与可选的 `weight_zero_point`（零点）。这正是 fp8 与 int4 处理链路最大的差别：fp8 量化在 trainer 上做完就能直接喂给 SGLang，而 int4 的打包/重排需要在**引擎侧**按内核期望的布局再做一次 `post_process`。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `slime_plugins/models/__init__.py` | 模型插件包的入口标记（空文件），表明插件按包目录组织、可被 import |
| `slime_plugins/models/glm4.py` | 最小的「spec 插件」示例：`get_glm_spec` 返回一个层规格 |
| `slime_plugins/models/glm5/glm5.py` | 复杂插件示例：`get_glm5_spec` 构建带 indexer 的自定义 MLA 注意力规格 |
| `tools/convert_hf_to_fp8.py` | **离线**工具：把 HF bf16 检查点一次性量化成 fp8（block/channel/tensor 三策略）并写回 config.json |
| `tools/convert_hf_to_int4_direct.py` | **离线**工具：把 HF 权重量化打包成 int4（AWQ 风格） |
| `slime/backends/megatron_utils/megatron_to_hf/__init__.py` | `convert_to_hf`：Megatron→HF 的「改名 + 去填充 + 量化」总流水线 |
| `slime/backends/megatron_utils/megatron_to_hf/processors/__init__.py` | `quantize_params`：按 `quant_method` 分发到 fp8 / int4 处理器 |
| `slime/backends/megatron_utils/megatron_to_hf/processors/quantizer_fp8.py` | **核心**：在线 fp8 量化处理器 `quantize_params_fp8` |
| `slime/backends/megatron_utils/megatron_to_hf/processors/quantizer_compressed_tensors.py` | 在线 int4 量化处理器（compressed-tensors） |
| `slime/backends/megatron_utils/kernels/fp8_kernel.py` | 自带 Triton 内核 `blockwise_cast_to_fp8_triton` |
| `slime/backends/megatron_utils/sglang.py` | SGLang 依赖的薄封装：转出 ue8m0 量化工具，带 ImportError 兜底 |
| `slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py` | 权重同步主循环：量化发生在「逐 chunk 转换」这一步 |
| `slime/backends/megatron_utils/actor.py` | 在 `update_weight` 装配时从 `hf_config.quantization_config` 取量化配置 |

---

## 4. 核心概念与源码讲解

### 4.1 模型插件机制：`slime_plugins/models` 的组织与挂载

#### 4.1.1 概念说明

u4-l5 已经讲过，slime 把「搭 Megatron 模型」的工厂写在 `slime/backends/megatron_utils/model_provider.py` 的 `get_model_provider_func` 里，并提供 `--spec`（换层规格）、`--custom-model-provider-path`（整体替换工厂）两个定制入口。但当模型结构足够新（例如全新的注意力机制、indexer 模块），把这些自定义代码直接塞进框架本体既不优雅也难维护。于是 slime 用一个**第一方插件包** `slime_plugins/models/` 来集中存放「按模型族组织的新结构」。

它有两个关键特征：

1. **它是一个被打包的 Python 包**，而不是普通目录。`setup.py` 的 `include=["slime*", "slime_plugins*"]` 会把它一起装进 site-packages，于是你可以直接用 import 路径（如 `slime_plugins.models.glm4.get_glm_spec`）引用它，而不必关心代码树在哪。
2. **它的入口 `__init__.py` 是空的**——这不是疏漏，而是刻意的「目录式注册」：插件按模型族分散在各自的子文件/子目录里，框架不做集中注册表，而是靠 import 路径按需加载。

打开包入口可以看到它确实只是一个标记：

[slime_plugins/models/__init__.py:1](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime_plugins/models/__init__.py#L1) —— 空文件，仅用于让 `slime_plugins/models/` 成为一个可 import 的包。

包里按模型族分散着各类插件，例如 `glm4.py`、`glm5/`（子包，含自定义算子 `ops/`）、`qwen3_5.py`、`qwen3_5_vl.py`、`qwen3_next.py`、`minimax_m2.py`，以及若干可复用的注意力实现（`hf_attention.py`、`learnable_softmax_attention.py`、`flash_dot_product_attention.py`）。

#### 4.1.2 核心流程

模型插件最终都通过 u4-l5 讲过的两个挂载点之一接入工厂。流程是：

```
命令行 --spec slime_plugins.models.glm4.get_glm_spec
        （或 --custom-model-provider-path 某个 provider 函数）
        │
        ▼
get_model_provider_func(args)            # u4-l5 详讲
        │
        ├─ 若 args.spec 非空：import_module(args.spec) 得到一个对象
        │     ├─ 若它是「返回 spec 的函数」：调用它得到 spec，再交给 GPTModel
        │     └─ 若它本身就是「带 pre_process 的 provider」：直接委托它搭模型
        │
        └─ 若 args.custom_model_provider_path 非空：load_function 加载并调用
```

关键在于：slime 并没有为插件发明新机制，而是复用了 `--spec` 与 `--custom-model-provider-path`，让插件包里的函数以**普通 import 路径**的形式被引用。`slime_plugins/models/` 只是「官方把这些常用插件预先写好、打包随仓库分发」而已。

#### 4.1.3 源码精读

先看最小插件 `glm4.py`——它示范了「spec 插件」的标准写法：

[slime_plugins/models/glm4.py:4-14](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime_plugins/models/glm4.py#L4-L14) —— `get_glm_spec(args, config, vp_stage)` 只是薄薄地包了一层 Megatron 自带的 `get_gpt_layer_with_transformer_engine_spec`，多传了 `post_self_attn_layernorm` / `post_mlp_layernorm` 两个 GLM 专属开关。用 `--spec slime_plugins.models.glm4.get_glm_spec` 即可挂载。

这个签名 `(args, config, vp_stage) -> spec` 正好匹配工厂里对「可调用 spec」的调用约定：

[slime/backends/megatron_utils/model_provider.py:104-118](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L104-L118) —— `import_module(args.spec)` 拿到对象后，若它 `callable`，就以 `transformer_layer_spec(args, config, vp_stage)` 调用；甚至还允许它返回一个「完整的 provider」（带 `pre_process` 参数），从而把模型搭建整条委托给插件。

再看复杂插件 `glm5.py`，体会插件机制为何必要：GLM-5 系列用的是带 **indexer**（稀疏检索）的自定义 MLA 注意力，Megatron 原生根本没有这种层。于是插件需要自定义一整套 `DSAMLASelfAttention` 模块、自定义子模块清单 `DSASelfAttentionSubmodules`（多出 `wq_b` / `wk` / `k_norm` / `weights_proj` 四个 indexer 投影），再把它们逐层替换进标准 block spec：

[slime_plugins/models/glm5/glm5.py:707-777](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime_plugins/models/glm5/glm5.py#L707-L777) —— `get_glm5_spec` 从 HF config 读出 `index_n_heads` / `index_head_dim` 等新字段，构建 `DSAMLASelfAttention` 的 `ModuleSpec`，再循环把每个 decoder 层的 `self_attention` 换成它。这种规模的定制显然不适合塞进框架核心，于是以插件形式存在。

> 备注：`get_glm5_spec` 里的 `config.index_topk_freq` / `index_skip_topk_offset` 实现了「跨层索引共享」（部分 computing 层算 top-k，skip 层复用），并显式禁止跨流水线阶段的 top-k 共享（[slime_plugins/models/glm5/glm5.py:733-750](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime_plugins/models/glm5/glm5.py#L733-L750)）。本讲只需看到「插件可以承载任意复杂的结构创新」即可，细节不影响低精度主线。

`--custom-model-provider-path` 那条路（整体替换工厂）同样可以指向插件包里的函数，工厂侧的接驳点在这里：

[slime/backends/megatron_utils/model_provider.py:60-84](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L60-L84) —— 用 `load_function` 加载自定义 provider，探测它是否接受 `vp_stage` 形参，并在 critic 角色时把输出层换成 `LinearForLastLayer`（u4-l5 详述）。

参数本身的定义与签名约束在参数中枢：

[slime/utils/arguments.py:226-236](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L226-L236) —— `--custom-model-provider-path` 要求签名 `def custom_model_provider(pre_process, post_process, vp_stage=None) -> GPTModel`，值是 import 路径字符串。

#### 4.1.4 代码实践

**实践目标**：亲手走一遍「插件以 import 路径被挂载」的全链路，确认插件包与工厂的接驳点。

**操作步骤**：

1. 打开 `slime_plugins/models/glm4.py`，确认 `get_glm_spec` 的形参是 `(args, config, vp_stage)`、返回一个 spec 对象。
2. 打开 `slime/backends/megatron_utils/model_provider.py` 第 104–118 行，确认工厂对 callable spec 的调用签名正是 `(args, config, vp_stage)`——两边的约定必须严丝合缝。
3. 在 `scripts/models/` 下任选一个 `*.sh`，搜索是否出现 `--spec slime_plugins.models.` 字样，观察真实启动脚本如何引用插件。

**需要观察的现象**：插件的「注册」不需要任何装饰器或注册表——只要文件存在于 `slime_plugins/models/` 下、且函数签名匹配工厂的调用约定，就能用 import 路径挂载。

**预期结果**：你会看到「插件 = 一个符合约定签名的普通 Python 函数 + 它所在的包恰好被 setup.py 打包」这一极简事实。slime 刻意不引入插件注册中心，而是把「发现」交给 Python 的 import 机制。

> 待本地验证：步骤 3 是否能在某个 `scripts/models/*.sh` 里搜到 `slime_plugins.models` 的引用，取决于该脚本对应的模型是否用了第一方插件；若无也不影响结论。

#### 4.1.5 小练习与答案

**练习 1**：如果某模型既用了 `--spec slime_plugins.models.glm4.get_glm_spec`，又设了 `--custom-model-provider-path`，会发生什么？

**参考答案**：以 `--custom-model-provider-path` 为准。看 [model_provider.py:60-84](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L60-L84)：工厂在函数开头就 `if getattr(args, "custom_model_provider_path", None)` 直接 `return wrapped_model_provider`，根本不会走到下面读 `args.spec` 的分支。换言之，整体替换工厂会令 spec 挂载点失效——这与 u6-l1 讲的「外层接口覆盖内层挂载点」是同一原则。

**练习 2**：为什么 `slime_plugins/models/__init__.py` 是空文件，而不是写一个 `register()` 之类的注册函数？

**参考答案**：因为 slime 选择「import 路径即注册」的极简方案。插件被 `setup.py` 打包后即可 `import slime_plugins.models.xxx`，框架用 `load_function` / `import_module` 按字符串路径加载（见 u6-l1 的 `load_function`）。空 `__init__.py` 只是把目录变成包，不需要集中注册表。

---

### 4.2 fp8 / int4 权重转换：离线工具与在线流水线

#### 4.2.1 概念说明

低精度推理在 slime 里有**两条独立但同源的路径**：

- **离线路径**：训练开始前，用 `tools/convert_hf_to_fp8.py`（或 `convert_hf_to_int4_direct.py`）把一份 HF bf16 检查点**一次性**量化成 fp8/int4，并把量化参数写进 `config.json` 的 `quantization_config` 字段。产物直接作为 SGLang 的初始权重与配置。
- **在线路径**：训练开始后，每轮 `update_weights` 把 trainer 上的 **bf16** 权重同步给 SGLang 时，**即时**量化成与离线产物一致的低精度格式再下发。

两条路径同源的体现是：**在线路径读取的量化配置，正是离线工具写进 config.json 的那个 `quantization_config`**。这是一个非常优雅的衔接——离线工具负责「声明这个引擎要 fp8/int4」，在线路径负责「按这个声明每轮现做」。

为什么在线路径必须「每轮现做」而不能复用离线产物？因为权重每轮都在 bf16 下被优化器更新，旧的 fp8 产物立刻就过期了；而训练又不能在低精度下进行（见前置知识）。所以唯一正确的做法是：训练端始终 bf16，同步时现量化。

#### 4.2.2 核心流程

整个低精度同步链路可以画成：

```
trainer 侧（bf16）                    引擎侧（fp8 / int4）
─────────────────                    ────────────────────
weights_backuper.get("actor")        ← 每轮训练后的最新权重（bf16，分片）
        │
        ▼
convert_to_hf(name, param, qconfig)
  ├─ 1. 去掉 module. 前缀、去 vocab 填充      ← 无损结构转换（必须 bf16）
  ├─ 2. _convert_to_hf_core：按模型族改名       ← Megatron 名 → HF 名
  └─ 3. quantize_params(qconfig)              ← 有损量化（bf16 → fp8/int4）
        │
        ▼ （得到带正确 HF 名的 fp8/int4 张量 + scale）
_send_hf_params → 经 NCCL/IPC/disk 下发 ──→  SGLang 引擎加载
                                              （int4 还需 post_process_weights 重排）
```

关键顺序：**先做无损的结构转换（bf16），再做有损的量化**。这一点是本讲练习题的核心，先记下。

#### 4.2.3 源码精读

**离线工具：`tools/convert_hf_to_fp8.py`**

它支持三种量化策略，由 `--strategy` 选择：

[tools/convert_hf_to_fp8.py:39-95](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_hf_to_fp8.py#L39-L95) —— `block_fp8`（逐块，默认配 `--block-size`）、`channel_fp8`（逐输出通道）、`tensor_fp8`（整张量）。三者都是「算缩放因子 → 截断到 fp8」的缩放量化，差别只在 \(s\) 的粒度。注意 `block_fp8` 末尾会把 scale reshape 成 `(n_tiles, k_tiles)` 并以 `weight_scale_inv` 命名。

工具只量化「该量化」的权重——Linear 权重，跳过 layernorm、embedding、router、lm_head 等：

[tools/convert_hf_to_fp8.py:126-152](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_hf_to_fp8.py#L126-L152) —— 一长串 `and "... not in key"` 守卫，决定哪些张量进 `quant_fp8`、哪些原样保留并记入 `modules_to_not_convert`。

最关键的一步是把它「声明」进 config.json，这便是与在线路径的接口：

[tools/convert_hf_to_fp8.py:182-237](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_hf_to_fp8.py#L182-L237) —— block/tensor 策略写入 `{quant_method: "fp8", fmt: "e4m3", activation_scheme: "dynamic", weight_block_size: [...]}`；channel 策略写入 `compressed-tensors` 风格配置；最后 `cfg["quantization_config"] = quantization_config` 落盘。

> int4 的离线工具 `tools/convert_hf_to_int4_direct.py` 用法类似（[tools/convert_hf_to_int4_direct.py:1-7](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_hf_to_int4_direct.py#L1-L7)），它产出 AWQ 风格的打包权重，依赖一个 `fake_int4_quant_cuda` 扩展。

**在线路径的总入口：`convert_to_hf`**

`convert_to_hf` 是 Megatron→HF 转换的总流水线，量化是它的最后一步：

[slime/backends/megatron_utils/megatron_to_hf/__init__.py:23-33](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/megatron_to_hf/__init__.py#L23-L33) —— 依次：剥 `module.` 前缀 → `remove_padding` 去 vocab 填充 → `_convert_to_hf_core` 按模型族改名（[L41-L66](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/megatron_to_hf/__init__.py#L41-L66) 分发到 glm4/deepseekv3/qwen3/llama 等）→ `quantize_params`。

注意：前两步（改名、去填充）**必须在 bf16 下完成**，因为它们要靠 HF 参数名与完整形状才能正确进行；量化 `quantize_params` 才是压低精度的有损步骤，放在最后。

`quantize_params` 按 `quant_method` 分发，这就是「离线声明 → 在线执行」的接合点：

[slime/backends/megatron_utils/megatron_to_hf/processors/__init__.py:6-22](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/megatron_to_hf/processors/__init__.py#L6-L22) —— `fp8` 走 `quantize_params_fp8`，`compressed-tensors`（int4）走 `quantize_params_compressed_tensors`，未知方法（如 mxfp4）直接透传 bf16。`quantization_config is None` 时整体跳过，即纯 bf16 推理。

**在线配置从哪来**

trainer 在装配 `weight_updater` 时，从 HF 模型配置里取出量化声明：

[slime/backends/megatron_utils/actor.py:169-175](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L169-L175) —— `quantization_config=getattr(self.hf_config, "quantization_config", None)`。这一行就是离线工具与在线路径的「交握点」：引擎加载的那份带 `quantization_config` 的 config.json，被 trainer 读到，决定每轮同步时如何量化。

#### 4.2.4 代码实践

**实践目标**：用 `tools/convert_hf_to_fp8.py` 在一个小模型上跑一次离线量化，观察它如何改写 config.json，建立「离线声明」的直觉。

**操作步骤**：

1. 准备一个小的 HF bf16 模型目录（如 Qwen3-0.6B）。
2. 运行（**待本地验证**，需 GPU 与合适依赖）：

   ```bash
   python tools/convert_hf_to_fp8.py \
     --model-dir /path/to/qwen3-0.6B \
     --save-dir /path/to/qwen3-0.6B-fp8 \
     --strategy block --block-size 128 128 --max-workers 1
   ```

3. 对比原目录与产物目录的 `config.json`，查看新增的 `quantization_config` 字段。
4. 用 `safetensors.safe_open` 打开产物里的任一 `.safetensors`，确认 Linear 权重的 dtype 变成了 `torch.float8_e4m3fn`，且同名多了 `...weight_scale_inv` 张量；而 `lm_head` / `embed` 仍是 bf16。

**需要观察的现象**：`config.json` 里出现 `"quant_method": "fp8"`、`"fmt": "e4m3"`、`"weight_block_size": [128, 128]`；safetensors 里 Linear 权重为 fp8、附 `weight_scale_inv`，非 Linear 权重保持 bf16。

**预期结果**：你亲眼看到「离线工具写进 config.json 的 `quantization_config`」长什么样，从而理解 trainer 端 `getattr(self.hf_config, "quantization_config", None)` 拿到的就是这个字典。

> 若没有 GPU，可改为源码阅读型实践：精读 [convert_hf_to_fp8.py:182-237](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_hf_to_fp8.py#L182-L237)，画出「策略 → `quantization_config` 字典内容」的对照表。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `convert_to_hf` 要先 `remove_padding` 和改名，最后才 `quantize_params`，而不是反过来先量化？

**参考答案**：因为量化是**有损且单向**的，而改名/去填充是**无损结构变换**。更关键的是，量化是**按 HF 参数名**筛选目标、且需要**完整（未分片、未填充）张量**才能正确算逐块缩放因子。若先量化：一是此时还是 Megatron 命名，量化器的名字匹配规则（见 4.3）会失配；二是张量可能还带 vocab 填充或并行分片，缩放因子会被填充零或分片边界污染。所以必须先把 bf16 张量「摆正」，再压精度。

**练习 2**：若 `hf_config.quantization_config` 为 `None`，在线路径会发生什么？

**参考答案**：什么都不发生——走纯 bf16 推理。看 [processors/__init__.py:7-9](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/megatron_to_hf/processors/__init__.py#L7-L9)：`quantization_config is None` 时直接 `return converted_named_params`，`convert_to_hf` 只做改名与去填充，权重以 bf16 原样下发。

---

### 4.3 quantizer_fp8 处理器：在线 fp8 量化的实现

#### 4.3.1 概念说明

本模块精读 `quantizer_fp8.py`——它是「在线 fp8 量化」的具体实现，回答两个问题：

1. **量化谁**：不是所有参数都该压成 fp8。embedding、layernorm、output layernorm 这些精度敏感、且不是大块 GEMM 的参数应保持高精度；只有 attention 投影与 MLP 的 Linear 权重（以及 MoE 专家权重）才值得量化。处理器用一个**名字白名单**来筛选。
2. **怎么量化**：取决于是否有 `weight_block_size`。有则块级量化（精度高），无则 per-tensor 量化；块级量化内部又分两条子路径——SGLang 的 DeepGEMM ue8m0 路径，或 slime 自带的 Triton 内核。

#### 4.3.2 核心流程

```
quantize_params_fp8(megatron_name, params, qconfig)
  │
  ├─ 用正则解析 megatron_name：是 decoder.layers.N.* 还是 mtp.layers.N.* ？
  │     └─ 都不是（如 embedding/output）→ 直接返回原 bf16 参数
  │
  ├─ 在 rest（层内剩余路径）里匹配：
  │     ├─ 专家权重 mlp.experts.*.linear_fc1/fc2     → 量化
  │     ├─ 共享专家 mlp.shared_experts.linear_fc1/fc2 → 量化
  │     └─ 一组白名单（self_attention.linear_proj/linear_qkv、
  │                   mlp.linear_fc1/fc2、MLA 各投影、indexer、linear_attn）→ 量化
  │     └─ 其余（layernorm 等）→ 不量化
  │
  └─ 对每个命中的 weight 调 _quantize_param：
        ├─ 有 weight_block_size：
        │     ├─ should_deepgemm_weight_requant_ue8m0 为真 → SGLang ue8m0 路径
        │     └─ 否则 → blockwise_cast_to_fp8_triton（自带 Triton 内核）
        │     └─ scale 命名 weight_scale_inv
        └─ 无 weight_block_size → per-tensor：scale=absmax/FP8_MAX，命名 weight_scale
```

#### 4.3.3 源码精读

**名字筛选：量化谁**

`quantize_params_fp8` 的前半段全是「用正则给参数归类」：

[slime/backends/megatron_utils/megatron_to_hf/processors/quantizer_fp8.py:10-91](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/megatron_to_hf/processors/quantizer_fp8.py#L10-L91) —— 先用 `module.module.decoder.layers.(\d+)\.(.+)` 或 `mtp.layers.(\d+)\.(.+)` 判断是否是 transformer/MTP 层内的参数；不是则直接 `return converted_named_params`（即不量化，如 embedding）。进入层内后，再用一组正则把 `rest` 归到专家权重、共享专家、或一份明式白名单（`self_attention.linear_proj.weight`、`mlp.linear_fc1.weight`、MLA 的 `linear_q_proj`/`linear_q_down_proj`/`linear_kv_down_proj` 等、indexer 的 `wq_b`/`wk`、线性注意力的 `in_proj_qkv` 等）。

注意 [L44-L46](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/megatron_to_hf/processors/quantizer_fp8.py#L44-L46) 一个细节：专家权重量化时会 `continue` 跳过以 `_scale` 结尾的名字——这是因为上一道工序可能已经生成了 bf16 的 `weight_scale`/`input_scale`，量化时要把它们排除，注释里也写了 `TODO: find a clearer way`。

**量化内核：怎么量化**

`_quantize_param` 是真正的「bf16 → fp8」算子：

[slime/backends/megatron_utils/megatron_to_hf/processors/quantizer_fp8.py:94-113](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/megatron_to_hf/processors/quantizer_fp8.py#L94-L113) —— 分两条路：

- **有 `weight_block_size`**（块级量化）：先问 `should_deepgemm_weight_requant_ue8m0`。若为真，走 SGLang 的 `quant_weight_ue8m0` + `transform_scale_ue8m0`（DeepGEMM 期望的 ue8m0 缩放格式）；否则走 slime 自带的 `blockwise_cast_to_fp8_triton`。scale 一律命名 `weight_scale_inv`。
- **无 `weight_block_size`**（per-tensor）：\(s = \max(|w|)/448\)，截断到 \([-448, 448]\)，scale 命名 `weight_scale`。

`should_deepgemm_weight_requant_ue8m0`、`quant_weight_ue8m0`、`transform_scale_ue8m0` 都来自 SGLang，slime 用一个带兜底的薄封装导入：

[slime/backends/megatron_utils/sglang.py:1-8](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/sglang.py#L1-L8) —— `try/except ImportError` 把这三个符号从 `sglang.srt.layers.quantization.fp8_utils` 与 `sglang.srt.model_loader.utils` 转出，导入失败则置 `None`。这是 slime 复用上游 SGLang 量化逻辑、又不强耦合版本的标准手法（与 u1-l1 讲的「SGLang-native、上游升级零成本」一脉相承）。

当不走 ue8m0 路径时，用自带 Triton 内核做块级量化：

[slime/backends/megatron_utils/kernels/fp8_kernel.py:61-79](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/kernels/fp8_kernel.py#L61-L79) —— `blockwise_cast_to_fp8_triton` 把张量按 `(BLOCK_M, BLOCK_N)` 分块，对每块算 absmax → scale = absmax / fp8_max，再写入 fp8 张量与 scale 矩阵。其 Triton JIT 内核 [L24-L58](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/kernels/fp8_kernel.py#L24-L58) 里 `_absmax = tl.maximum(tl.max(tl.abs(x)), eps)`、`x_s = _absmax / fp8_max`、`y_q = tl.clamp(x * s_inv, fp8_min, fp8_max)` 正是前置知识里那条公式的逐块实现。

**int4 的不同：需要引擎侧 post_process**

int4（`compressed-tensors`）的处理器在 trainer 上把权重打包成 int32：

[slime/backends/megatron_utils/megatron_to_hf/processors/quantizer_compressed_tensors.py:266-295](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/megatron_to_hf/processors/quantizer_compressed_tensors.py#L266-L295) —— 产出 `weight_packed`（int32，每 32 bit 装 8 个 int4）、`weight_scale`、`weight_shape`，可选 `weight_zero_point`。

但 int4 还多一步 **引擎侧后处理**，这是它与 fp8 最大的差别。在 `update_weights` 主循环里，只有 `compressed-tensors` 才触发 `post_process_weights`：

[slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py:286-329](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L286-L329) —— 载入权重**前**调用 `post_process_weights(restore_weights_before_load=True, post_process_quantization=False)`，载入**后**再调用 `post_process_weights(restore_weights_before_load=False, post_process_quantization=True)`。fp8 完全不进这两个分支（`if ... in ["compressed-tensors"]` 守卫）。

`post_process_weights` 本身只是对每个引擎发一次 HTTP 远程调用：

[slime/backends/megatron_utils/update_weight/update_weight_from_distributed.py:358-374](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_distributed.py#L358-L374) —— 遍历 `rollout_engines` 调 `engine.post_process_weights.remote(...)`。引擎侧（SGLang）才真正知道自己的 int4/fp4 内核要什么布局，所以这步必须在引擎上做。这呼应了 u5-l3 讲的「换权重四步仪式 pause → flush → 发权重 → continue」——`post_process_weights` 正是嵌在「发权重」前后、对低精度场景的额外步骤。

**回答「fp8 权重在 update_weight 哪一步被生成」**

现在可以精确回答实践任务的后半问了。看权重同步主循环：

[slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py:276-331](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L276-L331) —— `update_weights()` 的流程是：`pause_generation` → `flush_cache` → 逐 chunk `get_hf_weight_chunks` → 每个 chunk 经 `_send_hf_params` 下发 → 最后 `continue_generation`。**fp8 权重就是在「逐 chunk 转换」这一步生成的**：每个 chunk 内部调用 `convert_to_hf(..., self.quantization_config)`（非专家走 [update_weight_from_tensor.py:236](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L236) 的专家分支同理），其末尾的 `quantize_params_fp8` 把 bf16 现压成 fp8，随后 `_send_hf_params` 立即把这个 fp8 张量 + scale 下发给引擎。也就是说：**fp8 权重在 trainer 内存里、按 chunk、在 `_send_hf_params` 之前的转换环节即时生成，不落盘**。专家权重也走同一条 `convert_to_hf(..., quantization_config)`（[L233-L244](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L233-L244)）。

> 这也解释了「为何 Megatron 检查点仍需 bf16 转换」：检查点里的权重是按 Megatron 并行分片、带 vocab 填充、用 Megatron 命名的 bf16 张量。要得到引擎能用的 fp8 权重，必须先把它无损地「改名 + 去填充 + 聚合回完整张量」（bf16 阶段），才能正确量化。bf16 转换是量化的**前提**，量化是叠在它之上的有损末步。

#### 4.3.4 代码实践

**实践目标**：跟踪一次「bf16 → fp8」在线量化在 `update_weights` 内的精确位置，并能指认 fp8 张量诞生在哪一行。

**操作步骤**：

1. 打开 `update_weight_from_tensor.py`，定位 `update_weights`（L276 起）。
2. 找到「逐 chunk 下发」的循环（`get_hf_weight_chunks` → `_send_hf_params`），确认每个 chunk 进 `_send_hf_params` 之前已被 `convert_to_hf` 处理。
3. 顺 `convert_to_hf` → `quantize_params` → `quantize_params_fp8` → `_quantize_param` 一路读下去，标注：bf16 在哪一行被读入、fp8 + scale 在哪一行被产出（[quantizer_fp8.py:102-112](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/megatron_to_hf/processors/quantizer_fp8.py#L102-L112)）。
4. 对比 int4 路径：在主循环里找出 `post_process_weights` 的两处调用（L286-L291 与 L322-L329），确认 fp8 路径**不会**触发它们。

**需要观察的现象**：fp8 的量化产物（`qweight` + `weight_scale_inv`）在 trainer 进程内、`_send_hf_params` 之前就已就绪；int4 则额外需要引擎侧 `post_process_weights` 才能被内核使用。

**预期结果**：你能用一句话回答——「fp8 权重在 `update_weights` 的逐 chunk 转换环节（`convert_to_hf` 末尾的 `quantize_params_fp8`）即时生成，随即由 `_send_hf_params` 下发；整个过程在内存中完成、不落盘」。

> 若想加深对量化数学的体感，可在本地用 PyTorch 复现 per-tensor 路径（示例代码，非项目代码）：
>
> ```python
> # 示例代码：手工复现 _quantize_param 的 per-tensor 分支
> import torch
> FP8_MAX = torch.finfo(torch.float8_e4m3fn).max  # 448.0
> w = torch.randn(128, 128, dtype=torch.bfloat16, device="cuda")
> scale = w.abs().max().clamp(min=1e-12).to(torch.float32) / FP8_MAX
> q = (w / scale).clamp(-FP8_MAX, FP8_MAX).to(torch.float8_e4m3fn)
> w_rec = (q.to(torch.float32) * scale).to(torch.bfloat16)
> print((w - w_rec).abs().mean())   # 观察量化误差
> ```

#### 4.3.5 小练习与答案

**练习 1**：为什么 fp8 不需要 `post_process_weights`，而 int4 需要？

**参考答案**：fp8（e4m3）是一种「原生浮点」格式，trainer 侧 `quantize_params_fp8` 产出的 `qweight (fp8) + scale` 就是 SGLang fp8 内核最终消费的形态，无需进一步重排。而 int4 是「打包整数」，trainer 把 8 个 4-bit 值塞进 int32（`weight_packed`），但具体 GEMM 内核（AWQ/Marlin 等）对权重布局、零点、通道顺序有各自的特殊要求，这些「按内核定制」的重排只能在引擎侧完成——引擎才知道自己用哪个内核。所以 int4 多了引擎侧 `post_process_weights(restore/post_process)` 两步，fp8 不进这个分支。

**练习 2**：`should_deepgemm_weight_requant_ue8m0` 为真时走 SGLang 的 ue8m0 路径，否则走自带 Triton 内核。为什么要有两套实现？

**参考答案**：因为不同的 SGLang/DeepGEMM 版本与硬件对「块级 fp8 缩放因子的编码格式」要求不同。ue8m0（无偏置的 8 位指数缩放）是 DeepGEMM 某些内核期望的格式，需要用 SGLang 的 `quant_weight_ue8m0` + `transform_scale_ue8m0` 专门生成；当不满足该内核的条件时，回退到 slime 自带的、产出标准 fp32 scale 的 `blockwise_cast_to_fp8_triton`。两套实现保证 slime 在不同上游版本下都能产出引擎可用的块级 fp8 权重——这正是 [sglang.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/sglang.py#L1-L8) 用 `try/except ImportError` 兜底导入这些符号的原因。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「端到端追踪」任务。

**场景**：你要给一个 bf16 训练的 GLM 模型启用 fp8 推理，向同事解释清楚「从训练 checkpoint 到引擎里那份 fp8 权重」的完整链路，并指出每一步发生在哪个文件。

**任务**：

1. **离线准备**：说明你会先用哪个工具、用什么命令把初始 HF 检查点量化成 fp8，并指出它写进 `config.json` 的关键字段（`quant_method`/`fmt`/`weight_block_size`）。引用 [convert_hf_to_fp8.py:182-193](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_hf_to_fp8.py#L182-L193)。

2. **配置交握**：说明 trainer 如何「得知」要 fp8——指出 [actor.py:174](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L169-L175) 从 `hf_config.quantization_config` 取值，这正是上一步写进去的字段。

3. **在线量化定位**：在 `update_weights` 主循环里精确指出 fp8 权重诞生在哪一步（逐 chunk 的 `convert_to_hf` → `quantize_params_fp8`），并说明它是内存中现做、不落盘。引用 [update_weight_from_tensor.py:299-310](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L299-L310) 与 [quantizer_fp8.py:94-113](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/megatron_to_hf/processors/quantizer_fp8.py#L94-L113)。

4. **模型结构插件**：如果该 GLM 用了自定义注意力（如 GLM-5 的 DSA），说明你会用 `--spec slime_plugins.models.glm5.get_glm5_spec` 挂载插件，并指出接驳点是 [model_provider.py:104-118](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L104-L118)。

5. **回答关键问题**：用 2–3 句话说明「为何 Megatron 检查点仍需 bf16 转换」与「fp8 在 update_weight 哪步生成」。

**参考要点（第 5 问）**：训练永远在 bf16 下更新权重（低精度无梯度通路），所以检查点是 bf16；要得到 fp8，必须先无损地把 Megatron 的分片/带填充/带前缀的 bf16 张量「改名 + 去填充 + 聚合」成 HF 命名的完整张量，才能正确量化——bf16 转换是量化的前提。fp8 权重在 `update_weights` 的逐 chunk 转换环节（`convert_to_hf` 末尾的 `quantize_params_fp8` → `_quantize_param`）即时生成，随即由 `_send_hf_params` 下发给引擎，全程在内存中、不落盘。

## 6. 本讲小结

- `slime_plugins/models/` 是第一方**模型插件包**，入口 `__init__.py` 为空、按模型族分散组织；插件以普通 import 路径通过 `--spec`（返回层规格的函数）或 `--custom-model-provider-path`（整体 provider）挂载，框架不设集中注册表。
- 低精度推理有**两条同源路径**：离线工具（`convert_hf_to_fp8.py` 等）在训练前把 HF 权重一次性量化并写 `config.json` 的 `quantization_config`；在线路径每轮 `update_weights` 时按这份声明即时把 bf16 量化成低精度。
- 在线量化的总流水线是 `convert_to_hf`：**先做无损的改名/去填充（bf16），再做有损的 `quantize_params`**，顺序不可颠倒，因为量化依赖正确的 HF 命名与完整张量。
- `quantizer_fp8.py` 用正则名字白名单筛选要量化的 Linear 权重（attention 投影、MLP、MoE 专家、共享专家、MLA/indexer/linear-attn），embedding 与 layernorm 不量化；块级量化在 DeepGEMM ue8m0 路径与自带 Triton 内核 `blockwise_cast_to_fp8_triton` 之间二选一。
- fp8 与 int4 的关键差别：fp8 在 trainer 侧做完即可直接喂引擎；int4（compressed-tensors）还须在引擎侧 `post_process_weights`（载入前 restore、载入后 post_process）做按内核定制的重排。
- 「bf16 训练 + fp8 推理」中，fp8 权重在 `update_weights` 的逐 chunk 转换环节即时生成、内存中完成、不落盘；Megatron 检查点必须先做 bf16 结构转换，才能正确量化。

## 7. 下一步学习建议

- 想看清「逐 chunk 下发」与 NCCL/IPC/disk 三种传输的配合，继续读 [u5-l2 三种权重传输](u5-l2-weight-transport-modes.md)，把本讲的「量化在哪一步」与「量化后的张量怎么送达引擎」连起来。
- 想了解引擎侧接收权重时的 `pause/flush/update/continue` 仪式与 `post_process_weights` 的 HTTP 落点，复习 [u5-l3 SGLang 引擎封装与生命周期](u5-l3-sglang-engine-wrapper.md)。
- 想扩展自定义量化策略，可以从 `slime/backends/megatron_utils/megatron_to_hf/processors/__init__.py` 的 `quantize_params` 分发处入手，仿照 `quantizer_fp8.py` 增加新的 `quant_method` 处理器，并对照 `tests/plugin_contracts/`（见 [u8-l6 测试、契约测试与 CI](u8-l6-tests-contracts-ci.md)）补一个签名自检。
- 若对自定义模型结构本身（如 GLM-5 DSA 注意力）感兴趣，可深读 `slime_plugins/models/glm5/` 下的 `ops/` 自定义算子与 `get_glm5_spec` 的层替换逻辑。
