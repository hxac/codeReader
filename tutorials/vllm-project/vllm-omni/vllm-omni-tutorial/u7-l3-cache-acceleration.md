# 缓存加速：TeaCache / Cache-DiT / MagCache

## 1. 本讲目标

扩散（Diffusion）模型的去噪过程是「多步重复前向」：DiT（Diffusion Transformer）要把同一条 latent 反复送进 Transformer 几十次。研究发现，相邻去噪步之间，Transformer 各 block 的输出往往非常接近——也就是说，大量计算是「重复劳动」。**缓存加速**的核心思想就是：**当某一步的输出和上一步足够像时，直接复用上一步缓存下来的「残差（residual）」，跳过昂贵的前向计算**。

vLLM-Omni 在 `vllm_omni/diffusion/cache/` 下提供了一套统一的缓存加速框架，把多种学术界方案（TeaCache、Cache-DiT、MagCache）收编到同一个接口背后。本讲学完后你应该能够：

1. 说出 `CacheBackend` 抽象类的三方法契约 `enable / refresh / is_enabled`，并解释它们在 worker 生命周期里被调用的时机。
2. 读懂 `get_cache_backend` 选择器如何用一个字符串把请求路由到不同后端实例。
3. 理解 TeaCache 的「时间步感知」判定（相对 L1 距离 + 多项式重缩放 + 累积阈值），以及它如何借助 `HookRegistry` **不改动任何模型代码**就劫持 `forward`。
4. 区分 Cache-DiT 的三种子策略 DBCache / SCM / TaylorSeer，以及它对「双 transformer」模型（如 Wan2.2）的支持方式。
5. 理解 MagCache 的「幅度（magnitude）自适应」判定与前两者的关键差异。

本讲是进阶层 U5（Diffusion 模块）的延伸，**前置**是 u5-l3（Diffusion Worker 与模型加载），因为缓存后端正是在 `DiffusionModelRunner` 的初始化与每次前向里被装配和刷新的。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **扩散去噪循环**：一次文生图是把随机 latent 经过 `num_inference_steps`（比如 50）步去噪，每步都跑一次 `transformer.forward`，最后用 VAE 解码成图像（见 u5-l4）。
- **DiT 与 block**：扩散 Transformer 通常由一串结构相同的 `transformer_blocks`（ModuleList）组成，缓存加速主要作用在这些 block 层面。
- **CFG（Classifier-Free Guidance）**：为提升质量，扩散模型通常对「正向 prompt」和「负向 prompt」各跑一次前向再合并。缓存必须区分这两条分支，否则正负状态会互相污染。
- **抽象基类（ABC）与工厂/选择器模式**：用统一的抽象基类定义契约，用一个选择器函数按名字实例化具体子类。这是 vLLM-Omni 里反复出现的工程模式（参见 u7-l1 的注意力后端选择）。
- **猴子补丁与 forward 拦截**：在不修改模型源码的前提下，把模块的 `forward` 替换成「带缓存的版本」。本讲的 `HookRegistry` 就是一个工程化的 forward 拦截器。

> 术语速查：**residual（残差）** 指「本步输出 − 上一步输入」之类的差值，缓存它就等价于缓存「这一步对输入做了什么改变」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [vllm_omni/diffusion/cache/base.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/base.py) | 定义抽象基类 `CacheBackend`（统一契约） |
| [vllm_omni/diffusion/cache/selector.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/selector.py) | `get_cache_backend` 选择器，按名字路由到具体后端 |
| [vllm_omni/diffusion/data.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py) | `DiffusionCacheConfig`：所有后端共享的「大口袋」配置 |
| [vllm_omni/diffusion/hooks/base.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/hooks/base.py) | `HookRegistry` / `ModelHook`：透明 forward 拦截机制（TeaCache/MagCache 依赖） |
| [vllm_omni/diffusion/cache/teacache/](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/teacache/) | TeaCache 后端（backend/hook/state/config/extractors） |
| [vllm_omni/diffusion/cache/cachedit/](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/cachedit/) | Cache-DiT 后端，封装第三方 `cache_dit` 库 |
| [vllm_omni/diffusion/cache/magcache/](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/magcache/) | MagCache 后端（幅度自适应） |
| [vllm_omni/diffusion/worker/diffusion_model_runner.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_model_runner.py) | 运行时装配点：在哪 `enable`、在哪 `refresh` |
| [vllm_omni/diffusion/registry.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py) | `_NO_CACHE_ACCELERATION`：明确不支持缓存加速的模型名单 |

---

## 4. 核心概念与源码讲解

### 4.1 CacheBackend 抽象：统一的缓存生命周期

#### 4.1.1 概念说明

vLLM-Omni 里同时存在 TeaCache、Cache-DiT、MagCache 等好几套加速方案，它们的算法各不相同（有的比较「时间步嵌入」、有的比较「输出残差」、有的依赖外部库），但对**使用者和运行时**来说，它们应该长得一样：装上、每次生成前刷新、查它是否生效。于是项目用一个抽象基类 `CacheBackend` 把「安装—刷新—查询」这三件事固化成契约，所有后端都实现这三件事，**算法差异被关进子类内部**。

这是一种典型的「策略模式 + 抽象基类」：上层只依赖 `CacheBackend` 类型，不关心你装的是哪一个。

#### 4.1.2 核心流程

缓存后端在 worker 生命周期里有固定的三段式：

1. **构造**：worker 初始化时，由 `get_cache_backend(name, config)` 按名字 new 出具体后端实例。
2. **`enable(pipeline)`（只调一次）**：在 pipeline 模型加载完成后调用一次，把缓存逻辑「安装」到 transformer 上（TeaCache/MagCache 是注册 hook，Cache-DiT 是调用 `cache_dit.enable_cache`）。
3. **`refresh(pipeline, num_inference_steps)`（每次生成都调）**：清空上一轮留下的残差、重置计数器，必要时按新的 `num_inference_steps` 重建缓存上下文，保证本次生成从干净状态开始。
4. **`is_enabled()`**：任何时候查询这个后端是否已经成功安装。

> 关键点：`enable` 是**一次性的结构改造**（改 forward、装 hook），`refresh` 是**每轮的状态清零**。把这两件事分开，是因为同一份「被改造过的 pipeline」要服务很多次生成，不能每次都重新装一遍 hook。

#### 4.1.3 源码精读

抽象基类定义在 `base.py`，三个方法的语义很清晰：

[base.py:L33-L104](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/base.py#L33-L104) —— `CacheBackend`：构造时保存 `config` 并把 `enabled=False`；`enable` 和 `refresh` 是 `@abstractmethod`（子类必须实现）；`is_enabled()` 是具体方法，直接返回 `self.enabled`。其中 `enable` 的文档强调它「Called once during pipeline initialization」，`refresh` 强调「Called at the start of each generation」。

[base.py:L61-L75](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/base.py#L61-L75) 这段说明 `enable` 可以从 pipeline 里拿到 `pipeline.transformer` 和 pipeline 类名——这是所有后端「找到要加速的对象」的统一入口。

运行时装配点在 `DiffusionModelRunner.__init__`：

[diffusion_model_runner.py:L292-L304](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_model_runner.py#L292-L304) —— 先用 `get_cache_backend(...)` 拿到后端实例；如果该模型在 `_NO_CACHE_ACCELERATION` 黑名单里（见 [registry.py:L327-L331](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L327-L331)，目前是 `NextStep11Pipeline`、`AudioXPipeline`），就强制关掉缓存；否则调用 `self.cache_backend.enable(self.pipeline)` 完成一次性安装。

每次前向前的刷新在 `_refresh_cache_for_requests`：

[diffusion_model_runner.py:L411-L437](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_model_runner.py#L411-L437) —— 用 batch 里第一条请求的 `num_inference_steps` 调 `refresh`（批处理准入已保证同批请求步数一致，所以用第一条即可）；当步数为 `None` 且后端是 `tea_cache`/`step_cache` 时，退回用 pipeline 默认步数，因为 TeaCache 的 refresh 实际上不依赖这个值。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「enable 在 init、refresh 在每次前向前」这条生命周期。

1. 打开 `vllm_omni/diffusion/worker/diffusion_model_runner.py`，分别在文件内搜索 `self.cache_backend.enable` 与 `self.cache_backend.refresh`。
2. 观察到 `enable` 只出现在 `__init__` 内（第 304 行），而 `refresh` 出现在 `_refresh_cache_for_requests`（第 437 行）这个被每次前向调用的辅助函数里。
3. 再看 [diffusion_model_runner.py:L523-L529](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_model_runner.py#L523-L529)：仅当 `cache_backend == "cache_dit"` 且开启 `enable_cache_dit_summary` 时，前向后才打印缓存命中统计。**预期结果**：你会清楚地看到「一次性 enable + 每轮 refresh + 可选的 cache_dit 统计」三段式，与本节的流程图一一对应。能否运行取决于本地 GPU 环境，无法运行时此为「源码阅读型实践」。

#### 4.1.5 小练习与答案

- **练习**：为什么 `enable` 不能放进每次前向里调用，而必须只在初始化时调一次？
  **参考答案**：`enable` 会改造 pipeline 的结构（注册 hook、替换 forward、安装外部库的 cache adapter），这是有副作用且较重的操作。每次生成如果都重装，既浪费又会反复改写模块，甚至冲突。把「结构改造」与「状态清零」分离，能让同一份被改造的 pipeline 高效服务多次生成。
- **练习**：`is_enabled()` 默认返回 `self.enabled`，那 `enabled` 这个标志在哪里被置 `True`？
  **参考答案**：在每个具体后端的 `enable()` 末尾（例如 TeaCache 第 172 行、Cache-DiT 第 201 行、MagCache 第 121 行），安装成功后才置 `True`。

---

### 4.2 get_cache_backend 选择器与共享配置 DiffusionCacheConfig

#### 4.2.1 概念说明

选择器（selector）是一个**纯函数**：输入「后端名字 + 配置」，输出「一个后端实例或 None」。它把「用户传进来的字符串」翻译成「具体的 Python 对象」，是上层与各种后端实现之间的唯一耦合点。和 u7-l1 的注意力后端选择器一样，这种设计让「新增一种缓存算法」只需要：实现 `CacheBackend` 子类 + 在选择器里加一个 `elif` 分支。

与选择器配套的是 **`DiffusionCacheConfig`**：它不是「每个后端一份配置」，而是一个**共享的大口袋 dataclass**，把 TeaCache、Cache-DiT、MagCache、step_cache 的所有参数都塞进同一份字段表。用户传 dict 时只写关心的几个键，其余用默认值。这样用户切后端时配置可以平滑过渡。

#### 4.2.2 核心流程

选择逻辑：

1. 名字是 `None` 或 `"none"` → 返回 `None`（关闭缓存）。
2. 配置是 dict → 用 `DiffusionCacheConfig.from_dict(dict)` 转成对象。
3. 按名字分派：`"cache_dit"` → `CacheDiTBackend(config)`；`"tea_cache"` → `TeaCacheBackend(config)`；`"mag_cache"` → `MagCacheBackend(config)`；`"step_cache"` / `"stepcache"` / `"step_cache_dit"` → `StepCacheBackend(config)`。
4. 不认识的名字 → 抛 `ValueError`。

#### 4.2.3 源码精读

选择器本体非常短：

[selector.py:L11-L49](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/selector.py#L11-L49) —— `get_cache_backend`：注意它的文档字符串里就列清了四种后端的能力一句话概括（cache_dit / tea_cache / mag_cache / step_cache），第 34-35 行把 dict 转成 `DiffusionCacheConfig`，第 37-44 行是 `if/elif` 分派。

共享配置 `DiffusionCacheConfig` 把所有后端参数聚到一处，并按后端分组注释：

[data.py:L392-L486](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L392-L486) —— 注意默认值是「跨后端调优过的」：比如 `rel_l1_thresh=0.2`（TeaCache，注释说约 1.5× 加速）、`Fn_compute_blocks=1`（Cache-DiT，比上游默认 8 更适合单 transformer）、`max_warmup_steps=4`（为 Z-Image 这类 8 步蒸馏模型优化）。`from_dict` 用 dataclass 字段反射，只挑认识的键，多余的塞进 `_extra_params`：

[data.py:L491-L533](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L491-L533) —— `from_dict` + `__getattr__`，让漏写的键走默认、没定义的键也能靠 `__getattr__` 取到，体现了「大口袋」的宽容性。

> 补充：`data.py` 里的 `normalize_omni_diffusion_kwargs`（第 82-86 行）还支持用环境变量 `DIFFUSION_CACHE_BACKEND`（或旧名 `DIFFUSION_CACHE_ADAPTER`）兜底，这样不传 `cache_backend` 也能开缓存。

#### 4.2.4 代码实践（源码阅读型）

**目标**：亲手验证「换后端只需改一个字符串」。

1. 在 `selector.py` 里把第 37-44 行的 `if/elif` 抄成一张表：名字 → 后端类。
2. 对照 `DiffusionCacheConfig`（[data.py:L420-L486](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L420-L486)），标出每组参数属于哪个后端（TeaCache 是 `rel_l1_thresh/coefficients`；Cache-DiT 是 `Fn_compute_blocks` 等；MagCache 是 `mag_threshold` 等）。
3. **预期结果**：你会看到「用户写 `cache_backend="tea_cache"` + `cache_config={"rel_l1_thresh":0.3}`」时，`mag_threshold`、`Fn_compute_blocks` 等键虽然也存在但不会被 TeaCache 读取——它们只是安静地待在口袋里。这就是共享配置的代价与便利。

#### 4.2.5 小练习与答案

- **练习**：如果你在 `Omni(...)` 里同时传了 `cache_backend="tea_cache"` 和 `cache_config={"Fn_compute_blocks": 8}`（一个 Cache-DiT 专用参数），会发生什么？
  **参考答案**：不会报错。`Fn_compute_blocks=8` 会被 `from_dict` 当成已知字段写进 `DiffusionCacheConfig`，但 TeaCache 后端只读 `rel_l1_thresh/coefficients`，根本不看 `Fn_compute_blocks`，所以这个值被静默忽略——这是一个常见的「配错后端参数」陷阱，不会有任何加速效果也不会报错。

---

### 4.3 TeaCache：时间步感知缓存与 HookRegistry 透明注入

#### 4.3.1 概念说明

TeaCache（**T**imestep **E**mbedding **A**ware Cache）的核心观察：**相邻去噪步的「调制后输入（modulated input）」越相似，整条 transformer 的输出也越相似**。于是它不去比较昂贵的最终输出，而是从一个早期 block 偷看一份「调制后输入」，衡量它与上一步的差距；差距小就复用上一步缓存的残差，差距大就老老实实跑完整前向。

TeaCache 在 vLLM-Omni 里有一个工程亮点：**它完全不修改任何模型源码**。它用一个自研的 `HookRegistry` 把 transformer 的 `forward` 整个替换成「带缓存判断的版本」，所有模型相关差异都收进一个 `extractor` 函数里。模型开发者要支持 TeaCache，只需写一个「如何从我的模型里抽出 modulated_input」的 extractor，模型本身的 `forward` 一行都不用动。

#### 4.3.2 核心流程

TeaCache 在每一步前向的判定逻辑（核心是 `_should_compute_full_transformer`）：

1. 第一步（`cnt == 0`）：必定全量计算，并把残差存下来。
2. 之后每一步：
   - 计算「相对 L1 距离」：

     \[
     d_t = \frac{\|x_t - x_{t-1}\|_1}{\|x_{t-1}\|_1 + \epsilon}
     \]

     其中 \(x_t\) 是本步的调制后输入。
   - 用**模型专属的多项式**重缩放：\(\tilde{d}_t = p(d_t)\)，\(p\) 由 `coefficients`（一组多项式系数）经 `np.poly1d` 构造。
   - 累积：\(S_t = S_{t-1} + |\tilde{d}_t|\)。
   - 判定：若 \(S_t < \tau\)（`rel_l1_thresh`，默认 0.2）→ **复用缓存**（把上一步残差直接加到输入上）；否则 → **全量计算**并把累积值清零。

CFG 处理：缓存状态按分支隔离。开了 CFG-parallel 时，用 rank 判断本卡是 positive 还是 negative 分支；没开时用前向计数器的奇偶性来区分。两套状态互不污染。

#### 4.3.3 源码精读

后端入口 `TeaCacheBackend.enable`：

[teacache/backend.py:L123-L172](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/teacache/backend.py#L123-L172) —— 先看 pipeline 类名是否在 `CUSTOM_TEACACHE_ENABLERS`（[第 95-100 行](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/teacache/backend.py#L95-L100)，收录 Bagel/Flux2Klein/HunyuanImage3/SenseNovaU1 等需要特殊处理的模型）；不在就走默认路径：用 transformer 类名 + `rel_l1_thresh` + `coefficients` 构造 `TeaCacheConfig`，再调 `apply_teacache_hook`。

Hook 安装与 forward 拦截：

[teacache/hook.py:L255-L279](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/teacache/hook.py#L255-L279) —— `apply_teacache_hook` 拿到 `HookRegistry.get_or_create(module)` 再 `register_hook("teacache", hook)`。

[hooks/base.py:L174-L198](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/hooks/base.py#L174-L198) —— `HookRegistry.get_or_create` 是「透明注入」的关键：它把原始 `module.forward` 备份成 `_omni_original_forward`，然后把 `module.forward` 换成一个 `_WrappedForward` 包装器。从此任何对 `transformer(...)` 的调用都会先走 `registry.dispatch`。

核心判定 `_should_compute_full_transformer`：

[teacache/hook.py:L191-L238](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/teacache/hook.py#L191-L238) —— 这就是上面流程对应的代码：第 220-227 行算相对 L1 距离，第 230 行多项式重缩放，第 231 行累积，第 234 行与 `rel_l1_thresh` 比较决定 cache/compute。

而 `new_forward` 把「判定结果」落成具体行为：

[teacache/hook.py:L141-L189](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/teacache/hook.py#L141-L189) —— 「FAST PATH」是 `hidden_states = hidden_states + state.previous_residual`（直接复用残差），「SLOW PATH」是调 `ctx.run_transformer_blocks()` 全量算，并把 `(out - in).detach()` 存为新的残差。注意第 125-137 行的 CFG 分支隔离逻辑。

模型专属多项式系数表：

[teacache/config.py:L9-L75](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/teacache/config.py#L9-L75) —— `_MODEL_COEFFICIENTS`，每种 transformer 一组 5 元多项式系数（如 `QwenImageTransformer2DModel`、`FluxTransformer2DModel`、`ZImageTransformer2DModel`、`HunyuanImage3Pipeline` 等），它们是把「相对 L1 距离」映射成「是否该重算」的尺度校准。

TeaCache 的状态容器很简单：

[teacache/state.py:L13-L38](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/teacache/state.py#L13-L38) —— `TeaCacheState` 只存 `cnt`、`accumulated_rel_l1_distance`、`previous_modulated_input`、`previous_residual`、`previous_residual_encoder` 五样；`reset()` 把它们全清，这正是 `refresh` 要做的事。

#### 4.3.4 代码实践（可运行型）

**目标**：用 TeaCache 跑一次文生图，观察加速。

1. 按 u1-l2 安装好 vLLM-Omni 与对齐的 vLLM。
2. 运行官方脚本（开缓存）：
   ```bash
   python examples/offline_inference/text_to_image/text_to_image.py \
     --model Qwen/Qwen-Image \
     --cache-backend tea_cache
   ```
3. 再用相同 prompt 与步数跑一次**不带** `--cache-backend`（关闭缓存），用 `time` 对比两次耗时。
4. **预期结果**：开 TeaCache 后日志里出现 `TeaCache applied with rel_l1_thresh=0.2 ...`，整图耗时下降约 1.5×~2.0×；图像质量肉眼接近。若想更激进，可加 `--tea-cache-rel-l1-thresh 0.4`，加速更高但可能出现伪影。GPU 不可用时此为「源码阅读型实践」，可改为阅读 `docs/user_guide/diffusion/cache_acceleration/teacache.md` 的 Quick Start 段确认命令与参数表。

#### 4.3.5 小练习与答案

- **练习**：把 `rel_l1_thresh` 调大到 0.8，图像质量和速度分别会怎样变化？用 `_should_compute_full_transformer` 的逻辑解释。
  **参考答案**：阈值更大 → 累积值 \(S_t\) 更难超过阈值 → 更多步走「复用缓存」的快路径 → 速度更快，但每多复用一次都多累积一点偏差，图像更容易出现伪影、细节变差。反之调小到 0.1 则更保守、更接近原图、加速更少。
- **练习**：TeaCache 为什么需要 `coefficients` 这个模型专属的多项式？
  **参考答案**：不同架构的 transformer，其「调制后输入的相对 L1 距离」与「输出实际差异」之间的映射关系不同。多项式 \(p\) 是对这个映射的经验校准（多数由论文或 ComfyUI-TeaCache 调参得到），让单一的 `rel_l1_thresh` 阈值能在不同模型上都工作在合理区间。

---

### 4.4 Cache-DiT：DBCache / SCM / TaylorSeer 与双 transformer 支持

#### 4.4.1 概念说明

Cache-DiT 与 TeaCache 思路同源（都是「相邻步相似就缓存」），但它是一个**独立的第三方库**（`import cache_dit`），并且把加速拆成了三种可组合的子策略，还专门照顾「双 transformer」这类复杂结构：

- **DBCache（Dual Block Cache）**：动态块级缓存。用前若干个 block（`Fn_compute_blocks`）算「相邻步残差差异」，差异小于 `residual_diff_threshold` 就缓存后续 block 的输出；用 `max_continuous_cached_steps` 限制连续缓存步数，防止误差累积爆炸。
- **SCM（Step Computation Masking）**：步级掩码。直接用一个策略（`slow/medium/fast/ultra`）声明「这 28 步里只算 N 步、其余复用」，类似 LeMiCa/EasyCache 的固定调度，可叠加在 DBCache 之上。
- **TaylorSeer**：用泰勒展开「预报」未来 hidden state，跳过计算。官方明确**不适合少步蒸馏模型**，所以默认关闭。

vLLM-Omni 的 `CacheDiTBackend` 是一层**生命周期胶水**：它把上面这些 `cache_dit` 库的能力，封装进 `enable/refresh` 契约里，并处理「单 transformer 自动支持」与「双 transformer / 多 block-list 自定义」两种接入路径。

#### 4.4.2 核心流程

Cache-DiT 的 `enable` 有两条路径：

1. **自定义路径**：pipeline 类名在 `CUSTOM_DIT_ENABLERS`（`model_specific.py` 里注册，如 Wan2.2、LongCatImage、BAGEL）→ 调对应 enabler，它返回一个 `refresh` 回调。
2. **默认路径**（标准单 transformer 模型，如 Qwen-Image、Z-Image）：
   - `_maybe_build_block_adapter`：看 transformer 有没有声明 `_cache_dit_adapter_config`（描述 block 列表与 forward 签名）；有就用 `BlockAdapter` 包，没有就交给 `cache_dit` 自动探测。
   - `enable_cache_for_dit`：构造 `DBCacheConfig` + 可选的 `CalibratorConfig`（TaylorSeer），调 `cache_dit.enable_cache(...)` 把缓存逻辑装到 block 上。
   - 返回一个 `refresh_cache_context` 闭包。

`refresh` 比较特殊：它不是简单的状态清零，而是调 `cache_dit.refresh_context(transformer, num_inference_steps, ...)`，因为 SCM 的步掩码与步数强相关——**步数变了就要重新生成掩码**。所以 Cache-DiT 的 refresh 真的会用到 `num_inference_steps`（这点和 TeaCache 不同）。

#### 4.4.3 源码精读

后端类与两条 enable 路径：

[cachedit/backend.py:L171-L202](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/cachedit/backend.py#L171-L202) —— `CacheDiTBackend.enable`：第 187 行查 `CUSTOM_DIT_ENABLERS`（[第 27 行](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/cachedit/backend.py#L27) 定义，由 `model_specific.py` 在包加载时填充）；没有就走默认：先 `_maybe_build_block_adapter`（[第 134-160 行](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/cachedit/backend.py#L134-L160)）、再 `_maybe_get_cached_adapter_cls`，最后 `enable_cache_for_dit`。注意它把 enabler 返回的回调存进 `self._refresh_func`。

默认 enabler `enable_cache_for_dit`：

[cachedit/backend.py:L82-L131](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/cachedit/backend.py#L82-L131) —— 构造 `DBCacheConfig` 和 `calibrator_config`，调用 `cache_dit.enable_cache(...)`；找不到合适 adapter 时抛出清晰的 `ValueError`（第 116-123 行），告诉你模型需要声明 `_cache_dit_adapter_config`。

refresh 回调的构造：

[cachedit/backend.py:L49-L79](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/cachedit/backend.py#L49-L79) —— `_build_cache_context_refresh`：根据 `num_inference_steps` 是否「被 SCM 支持」（第 61 行，`>=8` 或在 `{4,6}`）决定是带掩码还是普通地调 `cache_dit.refresh_context`。

Cache-DiT 专属配置投影：

[cachedit/config.py:L34-L93](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/cachedit/config.py#L34-L93) —— `CacheDiTConfig` 从共享 `DiffusionCacheConfig` 投影出 Cache-DiT 关心的字段；`to_db_cache_config()`（[第 70-84 行](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/cachedit/config.py#L70-L84)）拼出第三方库要的 `DBCacheConfig`，`to_calibrator_config()`（[第 86-93 行](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/cachedit/config.py#L86-L93)）在开启 TaylorSeer 时才返回校准器配置。

双 transformer 与多 block-list 的设计动机见设计文档：

[docs/design/feature/cache_dit.md:L123-L181](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/feature/cache_dit.md#L123-L181) —— 讲 Wan2.2 的「单/双 transformer 自动检测」（按 `transformer_2` 是否存在切换）与 LongCatImage 的「一个 transformer 两个 block-list」（`transformer_blocks` + `single_transformer_blocks`），都要用 `BlockAdapter` 显式声明多份 block 列表与 forward pattern。`cache_summary`（[cachedit/backend.py:L30-L43](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/cachedit/backend.py#L30-L43)）会同时遍历 `transformer` 与 `transformer_2` 打印统计，体现「双 transformer」一等公民地位。

#### 4.4.4 代码实践（可运行型）

**目标**：对比 DBCache 默认配置与「DBCache + SCM」组合的效果。

1. 运行（默认 DBCache）：
   ```bash
   python examples/offline_inference/text_to_image/text_to_image.py \
     --model Qwen/Qwen-Image \
     --cache-backend cache_dit \
     --num-inference-steps 50
   ```
2. 再开启 SCM 掩码（更激进，对应在线写法 `--cache-config '{"scm_steps_mask_policy":"medium"}'`，以脚本实际支持的 CLI 参数为准）。
3. **预期结果**：日志出现 `Cache-dit enabled successfully on QwenImagePipeline` 与 `Enabling cache-dit on transformer: Fn=..., Bn=..., W=...`；开 SCM 后命中步数进一步减少、耗时再降，但图像质量随策略激进度下降。具体数值「待本地验证」。GPU 不可用时改为阅读 `docs/user_guide/diffusion/cache_acceleration/cache_dit.md` 的「Acceleration Methods」一节确认各参数语义。

#### 4.4.5 小练习与答案

- **练习**：`max_continuous_cached_steps`（默认 3）这个参数解决什么风险？
  **参考答案**：缓存是「用旧值顶替新计算」，连续缓存步数越多，误差就越滚越大。`max_continuous_cached_steps` 限制「最多连续复用几步」，到上限就强制重算一次刷新缓存，从而把误差累积控制在一定范围内；正因为它兜底了精度，默认的 `residual_diff_threshold` 才敢设到相对激进的 0.24。
- **练习**：Cache-DiT 的 `refresh` 与 TeaCache 的 `refresh` 对 `num_inference_steps` 的依赖有什么不同？
  **参考答案**：TeaCache 的 refresh 只是重置状态计数器，`num_inference_steps` 实际上没被用到（源码里它「accepted for interface consistency」）。Cache-DiT 的 refresh 真的会把 `num_inference_steps` 传给 `cache_dit.refresh_context`，因为 SCM 的步掩码必须按步数重新生成；步数变了不刷新就会用错掩码。

---

### 4.5 MagCache：幅度自适应缓存与三种后端的能力差异

#### 4.5.1 概念说明

MagCache（**Mag**nitude-based Cache）是第三种思路。它**不在 transformer 入口比较「输入相似度」**，而是给每个去噪步、每个 block 预先标定一个「幅度比例（mag_ratio）」，用这些比例去累积一个「误差幅度」；当累积误差还没超过 `threshold`、且连续跳过步数没超过 `max_skip_steps` 时，就复用上一步的残差。

与 TeaCache 相比，MagCache 的判定信号是**预标定的、按步按 block 的比例**（需要为每种模型校准一张 `mag_ratios` 表），而不是在线实时算的输入距离。它支持「校准模式（`mag_calibrate`）」——第一次跑时把比例测出来供后续推理使用。

#### 4.5.2 核心流程

MagCache 的跳过判定（`MagCacheHeadHook`）：

1. 有一个「保留期」`retention_step = retention_ratio * num_inference_steps`（默认 `retention_ratio=0.1`，即前 10% 的步强制计算，保证稳定）。
2. 过了保留期后，每步：
   - 取该步的标定比例 \(r_t\)（来自 `mag_ratios[step]`）。
   - 累积比例：\(R_t = R_{t-1} \cdot r_t\)。
   - 累积误差：\(E_t = E_{t-1} + |1 - R_t|\)。
   - 跳过条件：若 \(E_t \le \text{threshold}\)（默认 0.24）**且** 累积跳过步数 \(\le \text{max\_skip\_steps}\)（默认 5）**且** 已有缓存残差 → 复用残差；否则重算并把累积量清零。

模型专属的 `mag_ratios` 由 `MagCacheStrategy` 提供；当用户给的步数和标定步数不一致、且策略支持插值时，自动用 `nearest_interp` 重采样到当前步数。

#### 4.5.3 源码精读

后端入口：

[magcache/backend.py:L62-L121](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/magcache/backend.py#L62-L121) —— `MagCacheBackend.enable`：若用户没给 `mag_ratios` 且不在校准模式，就用 `get_strategy(transformer_type)` 取模型专属策略的 `mag_ratios`，并在步数不匹配时插值（第 85-91 行）；最后 `apply_mag_cache_hook(transformer, config, strategy)`。

跳过判定的核心：

[magcache/hook.py:L112-L141](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/magcache/hook.py#L112-L141) —— 第 123 行算保留步，第 126-128 行累积 `accumulated_ratio` 与 `accumulated_err`，第 130-135 行就是上面流程的三条件跳过判定。第 144 行起是「复用残差」的分支，由 `strategy.apply_residual` 落实到具体模型。

策略抽象：

[magcache/strategy.py:L23-L79](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/magcache/strategy.py#L23-L79) —— `MagCacheStrategy` 定义了「每种模型如何算残差、如何施加残差、提供默认 `mag_ratios`」的契约，新增模型只需实现这个抽象类。

**三种后端的能力差异对照**（本讲最小模块之一）：

| 维度 | TeaCache | Cache-DiT | MagCache |
|------|----------|-----------|----------|
| 判定信号 | 在线算「调制输入的相对 L1 距离」 | 相邻步 block 残差差异 + 可选 SCM 步掩码 | 预标定的「幅度比例」累积误差 |
| 依赖 | 自研 `HookRegistry` | 第三方 `cache_dit` 库 | 自研 hook + diffusers `TransformerBlockRegistry` |
| 模型专属参数 | 多项式 `coefficients` | block adapter / `Fn_compute_blocks` 等 | `mag_ratios`（每步每 block 一张表） |
| refresh 是否用步数 | 否（仅重置计数） | 是（SCM 掩码随步数变） | 否（但 enable 时按步数插值 ratios） |
| 复杂结构支持 | 单 transformer 为主 | **双 transformer / 多 block-list 一等支持** | 单 transformer 为主，靠 strategy 扩展 |
| 子策略可组合 | 单一策略 | DBCache + SCM + TaylorSeer 可组合 | 单一策略 + 校准模式 |

#### 4.5.4 代码实践（源码阅读型）

**目标**：理解 MagCache 的「保留期」与「累积误差」如何防止单次跳过头。

1. 打开 [magcache/hook.py:L106-L141](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/magcache/hook.py#L106-L141)。
2. 跟踪一个假想场景：`num_inference_steps=50`、`retention_ratio=0.1`、`threshold=0.24`、`max_skip_steps=5`。
   - 保留步 = 5，前 5 步必算。
   - 第 6 步起开始累积 `accumulated_err`，每跳过一步 `accumulated_steps +1`；一旦 `accumulated_err > 0.24` 或 `accumulated_steps > 5`，立刻重算并清零。
3. **预期结果**：你能解释「为什么 MagCache 不会无限跳步」——两个上限（误差阈值、连续跳过步数）共同把误差有界化。这是「源码阅读型实践」，无需运行。

#### 4.5.5 小练习与答案

- **练习**：MagCache 的 `mag_calibrate=True` 模式有什么用？
  **参考答案**：校准模式让模型第一次跑时**不跳步**、并把每步的幅度比例测出来，供后续推理作为 `mag_ratios` 使用。当某模型还没有现成的 `mag_ratios` 表、又不想手调时，可用它先校准再推理。
- **练习**：从「判定信号」角度，为什么 MagCache 比 TeaCache 更依赖「针对该模型预先调好的参数」？
  **参考答案**：TeaCache 的判定信号是**在线、自监督**的（每步实时算输入距离），换模型只需换多项式系数做尺度校准，鲁棒性较好；MagCache 直接消费一张「哪步该用多大比例」的预标定表，这张表必须针对该模型（甚至该步数）校准过，否则跳过决策就是错的。所以 MagCache 通常要为每个新模型单独校准。

---

## 5. 综合实践

把本讲三个后端串起来做一次对比实验（对应大纲指定的实践任务）。

**任务**：为同一个扩散模型分别启用 `tea_cache` 与 `cache_dit`，记录推理步数与耗时变化，并用 `selector.py` 解释两种后端是如何被选中的。

**操作步骤**：

1. 选一个支持缓存加速的模型（如 `Qwen/Qwen-Image`，确保不在 `_NO_CACHE_ACCELERATION` 黑名单，见 [registry.py:L327-L331](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L327-L331)）。

2. 跑三组实验（同 prompt、同 `num_inference_steps`，用 `time` 计时）：
   - 基线（无缓存）：
     ```bash
     python examples/offline_inference/text_to_image/text_to_image.py \
       --model Qwen/Qwen-Image --num-inference-steps 50
     ```
   - TeaCache：
     ```bash
     python examples/offline_inference/text_to_image/text_to_image.py \
       --model Qwen/Qwen-Image --num-inference-steps 50 \
       --cache-backend tea_cache
     ```
   - Cache-DiT：
     ```bash
     python examples/offline_inference/text_to_image/text_to_image.py \
       --model Qwen/Qwen-Image --num-inference-steps 50 \
       --cache-backend cache_dit
     ```

3. 记录每组的「整图耗时」「日志里的缓存命中信息」「肉眼图像质量」三栏，填一张对比表。

4. **用源码解释选中过程**：对 `tea_cache` 这组，回到 [selector.py:L37-L44](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/selector.py#L37-L44)，说明字符串 `"tea_cache"` 命中第 39 行 → 构造 `TeaCacheBackend`，随后在 [diffusion_model_runner.py:L292-L304](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_model_runner.py#L292-L304) 调 `enable`；对 `cache_dit` 这组，说明命中第 37 行 → 构造 `CacheDiTBackend`，走默认 `enable_cache_for_dit` 路径。

**预期结果**：两种缓存都比基线快（约 1.5×~3×，Cache-DiT 叠 SCM 可更激进），图像质量随激进度有不同程度下降；两组日志分别出现 `TeaCache applied ...` 与 `Cache-dit enabled successfully ...`，证明它们确实经由选择器命中了不同后端。具体加速倍数「待本地验证」。

> 若无 GPU：改为纯源码阅读型——把 [selector.py:L11-L49](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/selector.py#L11-L49) 的分派表抄下来，为 `cache_dit` / `tea_cache` / `mag_cache` 三种字符串分别画出「命中哪一行 → 构造哪个类 → 该类 enable 调用哪个底层机制（cache_dit.enable_cache / apply_teacache_hook / apply_mag_cache_hook）」的对照图。

## 6. 本讲小结

- vLLM-Omni 用抽象基类 `CacheBackend`（`enable/refresh/is_enabled`）把 TeaCache、Cache-DiT、MagCache、step_cache 等多种扩散加速方案收编到统一契约下，`enable` 在 worker 初始化时调一次（结构性安装），`refresh` 在每次生成前调（状态清零）。
- `get_cache_backend` 是一个纯函数选择器，靠一个字符串把请求路由到具体后端；所有后端共享一份「大口袋」配置 `DiffusionCacheConfig`，切后端只需改字符串。
- TeaCache 用自研 `HookRegistry` **不动模型源码**地劫持 `forward`，靠「调制输入的相对 L1 距离 + 模型专属多项式 + 累积阈值」在线判定是否复用残差，并按 CFG 分支隔离状态。
- Cache-DiT 封装第三方 `cache_dit` 库，提供 DBCache / SCM / TaylorSeer 三种可组合子策略，并对「双 transformer / 多 block-list」模型有一等支持；它的 `refresh` 真正依赖 `num_inference_steps`（SCM 掩码随步数变）。
- MagCache 用预标定的「幅度比例」累积误差来判定跳过，依赖每模型一张 `mag_ratios` 表（可校准），靠「误差阈值 + 最大连续跳过步数」双重上限保证有界。
- 四种后端中只有 `cache_dit` 在前向后支持打印命中统计（`enable_cache_dit_summary`），且 `_NO_CACHE_ACCELERATION` 名单（NextStep11、AudioX）会被强制关闭缓存。

## 7. 下一步学习建议

- **横向对比加速效果**：阅读 [benchmarks/diffusion/bench_attention_backends.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/bench_attention_backends.py) 与 u8-l3（基准测试与性能剖析），把缓存加速和注意力后端、量化等其它加速手段放在一起做系统性的吞吐/延迟对比。
- **为新模型接入缓存**：结合 u9-l1（添加新 Diffusion 模型）和 [docs/design/feature/cache_dit.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/feature/cache_dit.md) 的「Custom Architectures」一节，练习为一种自定义结构写 `BlockAdapter` 或 TeaCache `extractor`，并注册到对应 `CUSTOM_*_ENABLERS`。
- **理解缓存与并行的交互**：缓存加速常与序列并行（u7-l2）、CFG 并行（u7-l4）叠加使用。建议接着读 u7-l4，弄清 TeaCache 在 CFG-parallel 下如何用 rank 区分正负分支，以及 Cache-DiT 的 `has_separate_cfg` 如何参与 block adapter 的构造。
- **深入源码**：若对算法本身感兴趣，可精读 [teacache/hook.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/teacache/hook.py) 的 `new_forward` 全貌与 [magcache/hook.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/magcache/hook.py) 的 head/tail 双 hook 协作，体会「模型无关的通用判定」与「模型相关的 extractor/strategy」是如何被干净分离的。
