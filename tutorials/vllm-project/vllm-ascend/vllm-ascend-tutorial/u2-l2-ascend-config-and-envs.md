# 配置体系：AscendConfig 与 envs

## 1. 本讲目标

本讲承接 [u2-l1 NPUPlatform：平台核心能力](u2-l1-npuplatform-core.md)。上一讲我们看到 `NPUPlatform` 通过一组「钩子」回答 vLLM 的硬件询问，其中 `check_and_update_config` 是最重的配置阶段。那么用户传进来的那些 NPU 专属参数（要不要开平衡调度、要不要融合某个算子）到底在哪里被读取、解析、校验？

学完本讲你将能够：

1. 说清楚 vLLM 的 `--additional-config` 是如何把一段 JSON 送到 vllm-ascend 手里的。
2. 读懂 `AscendConfig` 如何把这段 JSON 拆成一个个子配置（`AscendCompilationConfig` / `EplbConfig` / `SchedulerConfig` 等）。
3. 掌握配置值的「三级优先级」：`additional_config` 显式值 → 环境变量 → 默认值，并理解正在进行的「环境变量迁移到 additional_config」。
4. 理解 `AscendConfig` 的单例缓存与生命周期（`init_ascend_config` / `get_ascend_config` / `clear_ascend_config`），以及它被 `check_and_update_config` 在哪一步调用。
5. 掌握 `envs.py` 如何用一个 `env_variables` 字典 + 模块级 `__getattr__` 实现「惰性求值」的环境变量管理。

## 2. 前置知识

- **配置对象（Config Object）**：把一组相关参数封装成一个 Python 类（带默认值、校验逻辑），而不是散落的全局变量。vLLM 内部大量使用这种模式（如 `VllmConfig`、`CacheConfig`）。`AscendConfig` 就是 vllm-ascend 自己的配置对象。
- **`additional_config`**：vLLM 给硬件插件预留的一块「自由配置区」。它本质上是一个 `dict`，可以是任意 JSON，vLLM 自己不解释它，完全交给插件解析。这是「可插拔硬件」思想在配置层的体现。
- **环境变量**：进程级的字符串配置（如 `export FOO=1`）。优点是部署时灵活、不改代码；缺点是类型只能是字符串、分散、难以在运行期统一校验。
- **惰性求值（lazy evaluation）**：直到第一次真正被访问时才计算值，而不是在 import 时就算好。本讲会看到 `envs.py` 用它来「按需读取环境变量」。
- **单例（singleton）**：整个进程里只存在一个实例的对象，通常用一个模块级变量 + 「不存在则创建」的函数来维护。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `vllm_ascend/ascend_config.py` | 定义 `AscendConfig` 及其全部子配置类，以及单例管理函数 `init_ascend_config` / `get_ascend_config` / `clear_ascend_config`。本讲的主角。 |
| `vllm_ascend/envs.py` | 集中管理所有 `VLLM_ASCEND_*` 与构建相关环境变量，用字典 + `__getattr__` 实现惰性求值。 |
| `vllm_ascend/platform.py` | `NPUPlatform.check_and_update_config` 在其中调用 `init_ascend_config(vllm_config)`，是 `AscendConfig` 的实际诞生地。 |

## 4. 核心概念与源码讲解

### 4.1 additional_config 通道与 AscendConfig 的诞生

#### 4.1.1 概念说明

vllm-ascend 有一大批 NPU 专属开关，它们不属于 vLLM 原生配置（vLLM 不认识「平衡调度」「NZ 权重布局」这些概念）。vLLM 给插件留了一个通用出口：`additional_config`——一个纯 `dict`，vLLM 只负责把它原样存进 `VllmConfig.additional_config`，不解释内容，由插件自己解析。

用户可以通过两种方式填它：

- **在线服务**：`vllm serve <model> --additional-config='{"key":"value"}'`，传一段 JSON 字符串。
- **离线推理**：`LLM(model, additional_config={"key": "value"})`，直接传 Python dict。

无论哪种，最终都汇入 `VllmConfig.additional_config` 这个 dict，等待插件读取。`AscendConfig` 就是 vllm-ascend 用来「读懂这块自由配置区」的翻译官。

#### 4.1.2 核心流程

`AscendConfig` 的诞生可以分成三步：

1. **取出来**：从 `vllm_config.additional_config` 取出 dict（为 `None` 时退化为 `{}`）。
2. **拆开**：按约定的 key（如 `ascend_compilation_config`、`eplb_config`、`scheduler_config`）把 dict 拆成若干子 dict。
3. **建子配置**：把每个子 dict 喂给对应的子配置类（负责填默认值 + 校验），挂到 `AscendConfig` 的属性上。

```text
VllmConfig.additional_config (dict)
        │  按 key 切片
        ├── "ascend_compilation_config" ──► AscendCompilationConfig(**sub)
        ├── "eplb_config"               ──► EplbConfig(sub)
        ├── "scheduler_config"          ──► SchedulerConfig(sub, ...)
        ├── "finegrained_tp_config"     ──► FinegrainedTPConfig(sub, ...)
        └── 顶层裸 key（enable_xxx 等）  ──► 直接 .get(key, default)
                                              │
                                              ▼
                                       AscendConfig 实例
```

#### 4.1.3 源码精读

`AscendConfig.__init__` 接收一个 `vllm_config`，第一步就是取出 `additional_config`：

[vllm_ascend/ascend_config.py:32-34](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py#L32-L34) — 取出 `additional_config`，为空时退化为 `{}`，保证后续 `.get()` 不会因 `None` 报错。

随后用 `.get(key, {})` 切出各个子 dict，再实例化子配置类。例如编译与 EPLB 子配置：

[vllm_ascend/ascend_config.py:40-50](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py#L40-L50) — 从 `additional_config` 切出 `ascend_compilation_config`、`ascend_fusion_config`、`finegrained_tp_config`、`eplb_config` 四个子配置并实例化。

注意第 52–57 行：`SchedulerConfig` 的构造还把环境变量 `VLLM_ASCEND_BALANCE_SCHEDULING` 作为兜底值传了进去（这就是后面要讲的「环境变量回退」）：

[vllm_ascend/ascend_config.py:52-57](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py#L52-L57) — `from vllm_ascend import envs as ascend_envs` 后，把 `ascend_envs.VLLM_ASCEND_BALANCE_SCHEDULING` 作为 `balance_env_value` 传给 `SchedulerConfig`。

这一行同时演示了 `envs.py` 的惰性求值：访问 `ascend_envs.VLLM_ASCEND_BALANCE_SCHEDULING` 会触发模块级 `__getattr__`，真正去读环境变量。

#### 4.1.4 代码实践

**实践目标**：直观验证 `additional_config` 是如何被 `AscendConfig` 解析的（源码阅读型，无需 NPU）。

1. 打开 [vllm_ascend/ascend_config.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py#L27)，找到 `__init__`。
2. 用一张表列出「顶层 key → 子配置类 → 属性名」的对应关系，至少覆盖 `ascend_compilation_config`、`eplb_config`、`scheduler_config`、`finegrained_tp_config`、`ascend_fusion_config`、`xlite_graph_config` 六项。
3. **预期结果**：你会看到所有 vllm-ascend 的「结构化配置」都通过 `.get(key, {})` 切片 + 子配置类实例化的同一种模式接入。这是本讲最重要的「套路」。

#### 4.1.5 小练习与答案

**练习 1**：如果用户给的 `additional_config` 里出现了一个 `AscendConfig` 不认识的顶层 key（比如 `{"my_custom_flag": true}`），会发生什么？

**参考答案**：不会报错。`AscendConfig.__init__` 只用 `.get(key, default)` 主动去取它认识的 key，不会遍历或拒绝未知 key。`my_custom_flag` 会被静默忽略（除非有别的代码显式去读它）。

**练习 2**：为什么 `AscendConfig` 构造时要写 `additional_config if additional_config is not None else {}`，而不是直接用 `vllm_config.additional_config`？

**参考答案**：vLLM 允许 `additional_config` 为 `None`（用户根本没传 `--additional-config`）。直接对 `None` 调用 `.get()` 会抛 `AttributeError`，所以必须先退化为空 dict。

---

### 4.2 AscendConfig 的子配置体系与「三级优先级」

#### 4.2.1 概念说明

`AscendConfig` 本身只是一个「壳」，真正的配置细节分散在若干子配置类里。每个子配置类都遵循同样的约定：

- 用 `_defaults` 字典或函数默认参数声明默认值；
- 在 `__init__` / `_validate` 里做类型与范围校验，不合法就直接 `raise`；
- 把值挂到自己身上作为属性。

更重要的是，很多开关同时存在「`additional_config` 值」和「同名环境变量」两个来源。vllm-ascend 正在把环境变量迁移到 `additional_config`（过渡期两者都支持，未来只保留 `additional_config`）。于是产生了一个**配置值优先级**问题。项目用一个统一函数 `_get_config_value` 来回答它。

#### 4.2.2 核心流程：三级优先级

读取一个「同时有环境变量和 additional_config 入口」的开关时，优先级如下（高 → 低）：

1. **`additional_config` 里的显式值**（最优先，也是推荐方式）。
2. **环境变量**（过渡期兜底，会打印 deprecation 警告）。
3. **代码里的默认值**（前两者都没给时使用）。

```text
_get_config_value(additional_config, config_key, env_key, env_value)
        │
        ├── config_key 在 additional_config 里?  ──► 是: 用它 (info 日志)
        │
        ├── env_key 在 os.environ 里?            ──► 是: 用 env_value (deprecation 警告)
        │
        └── 都没有                                  ──► 用 env_value 这个「默认值」
```

注意：第 3 步复用了第 2 步传入的 `env_value`，因为传进来的 `env_value` 本身就是 `envs.py` 算好的「环境变量值或默认值」。

#### 4.2.3 源码精读

`_get_config_value` 是 `AscendConfig` 的静态方法，集中实现了上面的三级优先级：

[vllm_ascend/ascend_config.py:294-306](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py#L294-L306) — 先看 `additional_config`，再看 `os.environ`，最后用传入的 `env_value`（默认值）。

它的一个典型调用点是 `enable_fused_mc2`：

[vllm_ascend/ascend_config.py:152-157](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py#L152-L157) — `enable_fused_mc2` 通过 `_get_config_value` 决定取 `additional_config.enable_fused_mc2`、环境变量 `VLLM_ASCEND_ENABLE_FUSED_MC2`，还是默认 `0`。

下面看三个代表性子配置类。

**① `AscendCompilationConfig`——控制图融合行为**，带默认参数 + `**kwargs`（向前兼容）：

[vllm_ascend/ascend_config.py:515-522](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py#L515-L522) — 四个开关 `enable_npugraph_ex` / `enable_static_kernel` / `fuse_norm_quant` / `fuse_qknorm_rope` 都带默认值，外加 `**kwargs` 兜住未来新增字段。

它还体现了「硬件分支」：310P 不支持 `npugraph_ex`，构造时会强制关掉：

[vllm_ascend/ascend_config.py:545-556](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py#L545-L556) — `is_310p()` 为真时，把 `enable_npugraph_ex` 与 `enable_static_kernel` 强制置为 `False` 并打印告警。

**② `EplbConfig`——专家负载均衡**，用 `_defaults` 字典 + `__getattr__` 模式：

[vllm_ascend/ascend_config.py:780-807](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py#L780-L807) — 默认值集中放在类级 `_defaults` 字典里，`__init__` 合并用户值，未知 key 直接 `raise ValueError`，再走 `_validate_config`。

注意 `__getattr__`（第 804 行）：访问 `self.dynamic_eplb` 会转去 `self.config["dynamic_eplb"]`，让属性访问像读普通字段一样自然。

**③ `SchedulerConfig`——调度扩展**，它内部又嵌套了 `ProfilingChunkConfig` / `ShortRequestFirstConfig` / `BatchJobSchedConfig` 等更小的子配置：

[vllm_ascend/ascend_config.py:873-903](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py#L873-L903) — `SchedulerConfig` 先取 `additional_config.scheduler_config` 子 dict，再分别构造 balance / recompute / short_request_first / profiling_chunk / batch_job 五项。

`SchedulerConfig` 自己也有一套「三级优先级」，甚至更细：它额外处理了「`additional_config` 顶层旧写法 → `scheduler_config` 子 dict 新写法」的迁移，并对旧写法打 deprecation 警告：

[vllm_ascend/ascend_config.py:904-941](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py#L904-L941) — `_get_config_value` 优先用 `scheduler_config` 子 dict，其次用 `additional_config` 顶层旧 key（警告），最后用环境变量（警告）。

> 子配置一览（速查）：
>
> | 子配置类 | additional_config key | 管什么 |
> |----------|----------------------|--------|
> | `AscendCompilationConfig` | `ascend_compilation_config` | 图融合、npugraph_ex、静态 kernel |
> | `AscendFusionConfig` | `ascend_fusion_config` | gmmswigluquant 融合算子 |
> | `FinegrainedTPConfig` | `finegrained_tp_config` | oproj / lmhead / embedding 等细粒度 TP |
> | `EplbConfig` | `eplb_config` | 专家负载均衡 |
> | `SchedulerConfig` | `scheduler_config` | 平衡调度、recompute、分块并行等 |
> | `XliteGraphConfig` | `xlite_graph_config` | XLite 分层推理图模式 |
> | `RejectionSamplerConfig` | `rejection_sampler_config` | 投机解码拒绝采样的 block/entropy verify |

#### 4.2.4 代码实践

**实践目标**：本讲的指定实践——写一段同时开启「balance scheduling」与某个 `ascend_compilation_config` 选项的 `additional_config`，并定位它被读取的位置。

1. 写出 JSON（在线服务用字符串形式）：

```json
{
  "scheduler_config": {
    "enable_balance_scheduling": true
  },
  "ascend_compilation_config": {
    "fuse_norm_quant": true
  }
}
```

离线推理等价写法（Python dict）：

```python
LLM(
    model="Qwen/Qwen3-8B",
    additional_config={
        "scheduler_config": {"enable_balance_scheduling": True},
        "ascend_compilation_config": {"fuse_norm_quant": True},
    },
)
```

2. 解释「在哪一步被读取」：
   - vLLM 把这段 JSON 存进 `VllmConfig.additional_config`；
   - `NPUPlatform.check_and_update_config` 内部调用 `init_ascend_config(vllm_config)`（见 [platform.py:431](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L431)）；
   - `AscendConfig.__init__` 在第 40 行切出 `ascend_compilation_config`、实例化 `AscendCompilationConfig(fuse_norm_quant=True)`；在 `SchedulerConfig`（第 881 行）里读取 `enable_balance_scheduling`。
3. **预期结果**：`get_ascend_config().ascend_compilation_config.fuse_norm_quant == True`，`get_ascend_config().scheduler_config.enable_balance_scheduling == True`。这一步在无 NPU 环境下无法真正跑起 `LLM(...)`，请标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：用户同时设置了环境变量 `export VLLM_ASCEND_ENABLE_FUSED_MC2=1` 和 `additional_config={"enable_fused_mc2": 0}`，最终生效的是哪个？

**参考答案**：`enable_fused_mc2=0`。`_get_config_value` 第一优先级是 `additional_config` 的显式值，环境变量只在 `additional_config` 没给时才作为兜底（并会打印「请改用 additional_config」的 deprecation 警告）。

**练习 2**：`AscendCompilationConfig` 的 `__init__` 为什么有一个 `**kwargs` 参数？

**参考答案**：为了**向前兼容**。当未来新增字段、但用户运行的是旧版本插件时，传入的未知字段会被 `**kwargs` 吞掉而不报错；同理新版本插件读取旧 `additional_config` 里没有的字段时也能用默认值。第 561 行的 `self.fuse_muls_add = kwargs.get("fuse_muls_add", True)` 就是这种用法。

---

### 4.3 AscendConfig 的单例缓存与生命周期

#### 4.3.1 概念说明

`AscendConfig` 构造代价不低（要解析大量子配置、读环境变量、做校验），而它在一个进程里本质上是「一份配置」。如果每次需要都重新构造，既浪费又会因为多次校验互相打架。因此项目用了**单例缓存**：用一个模块级变量 `_ASCEND_CONFIG` 缓存唯一实例，配三个函数管理它的生命周期。

- `init_ascend_config(vllm_config)`：构造并缓存（若已缓存且未要求刷新则直接返回旧的）。
- `get_ascend_config()`：只读访问，要求实例必须已初始化，否则报错。
- `clear_ascend_config()`：清空缓存（同时清掉相关的 `enable_sp` 缓存）。

这套机制还特别照顾了「一个进程里换模型/换配置重新加载」的场景（如 RLHF、UT/e2e 测试）。

#### 4.3.2 核心流程

```text
init_ascend_config(vllm_config)
        │
        ├── 已有缓存 且 未要求 refresh 且 实例完整 且 是同一个 vllm_config?
        │       └── 是: 直接返回缓存（命中）
        │
        └── 否: new_config = AscendConfig(vllm_config)
                ├── new_config 完整? ──► 更新缓存 _ASCEND_CONFIG
                └── 不完整(被 UT monkeypatch) ──► 警告，不污染缓存
        返回 new_config

get_ascend_config()  ──► 缓存为空/不完整: raise RuntimeError
clear_ascend_config() ──► _ASCEND_CONFIG = None; clear_enable_sp()
```

关键判断 `_is_ascend_config_initialized` 会检查实例是否拥有 `ascend_compilation_config` 与 `eplb_config` 两个属性——因为有些单元测试会 monkeypatch `AscendConfig.__init__` 跳过重型初始化，导致实例「半初始化」，此时不能把这种残缺实例放进缓存。

#### 4.3.3 源码精读

模块级缓存变量与三个生命周期函数集中在文件末尾：

[vllm_ascend/ascend_config.py:944-957](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py#L944-L957) — `_ASCEND_CONFIG` 是模块级单例槽位；`_is_ascend_config_initialized` 用两个关键属性判断实例是否「完整」。

`init_ascend_config` 是带 `refresh` 支持的「带缓存的构造器」：

[vllm_ascend/ascend_config.py:960-976](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py#L960-L976) — 读取 `additional_config.refresh`（第 962 行），命中缓存要同时满足四个条件；构造后只有完整实例才更新缓存。

`get_ascend_config` 与 `clear_ascend_config` 一读一清：

[vllm_ascend/ascend_config.py:979-991](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py#L979-L991) — `clear` 还顺带调用 `clear_enable_sp()` 清掉序列并行的派生缓存；`get` 在未初始化时直接 `raise RuntimeError`。

那么这个 `init_ascend_config` 是被谁调用的？答案是 `NPUPlatform.check_and_update_config`（承接 u2-l1 提到的「最重的配置阶段」）：

[vllm_ascend/platform.py:428-431](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L428-L431) — 先 `_fix_incompatible_config` 把 GPU 专属参数改写成安全值，再 `init_ascend_config(vllm_config)` 真正构造并缓存 `AscendConfig`。

注意这里的「时机」链条（u2-l1 已讲过三阶段，这里只做定位）：

1. `apply_config_platform_defaults`（[platform.py:247](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L247)）：注入默认值，**早于**校验。
2. `check_and_update_config`（[platform.py:408](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L408)）：第 431 行构造 `AscendConfig`，第 450–456 行把解析后的编译配置回写进 `additional_config` 供后续 pass 使用。
3. 后续 `_set_cudagraph_sizes()`（[platform.py:502](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L502)）：依据前面算好的值再推导图捕获尺寸。

也就是说，用户那段 `additional_config` JSON 是在**第 2 阶段（check_and_update_config）的第 431 行**被 `AscendConfig` 真正「读懂」的。

#### 4.3.4 代码实践

**实践目标**：理解 `refresh` 的作用——为什么 RLHF / 测试场景需要强制重建配置（源码阅读型）。

1. 阅读 `init_ascend_config`（第 960–976 行），找到判断「是否命中缓存」的四个条件。
2. 思考：在 RLHF 在线学习里，同一个进程会先用配置 A 加载模型推理，随后切换到配置 B 重新加载。如果不刷新，第二次拿到的会是哪份配置？
3. 给出修复方法：在 `additional_config` 里加 `"refresh": true`。
4. **预期结果**：设置 `refresh=true` 后，即使缓存里已有实例，`init_ascend_config` 也会用新的 `vllm_config` 重新构造一份并替换缓存。这一点在无 NPU 环境下为「待本地验证」，可通过阅读 [tests/ut/test_ascend_config.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/tests/ut/test_ascend_config.py) 中调用 `clear_ascend_config()` 的测试确认缓存机制。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `init_ascend_config` 命中缓存时还要检查 `getattr(_ASCEND_CONFIG, "vllm_config", None) is vllm_config`（是不是同一个 vllm_config 对象）？

**参考答案**：防止「进程内换了 `VllmConfig` 对象但忘了 refresh」时返回陈旧配置。如果用户构造了一个新的 `VllmConfig`（即便内容一样也是不同对象），单例应当为它重建，而不是返回绑定在旧对象上的实例。

**练习 2**：`get_ascend_config()` 和 `init_ascend_config()` 的区别是什么？什么场合该用哪个？

**参考答案**：`init` 负责「构造并缓存」（写），`get` 负责「只读访问」（读）。`platform` 启动时用 `init`；之后散落在 worker、ops、attention 等各处的代码只需要读配置，应当用 `get`，并在未初始化时让它抛错以暴露调用顺序错误（而不是悄悄重新构造）。

---

### 4.4 envs.py：环境变量的集中管理与惰性求值

#### 4.4.1 概念说明

除了 `additional_config`，vllm-ascend 还有一批「构建期 / 运行期」环境变量（如 `SOC_VERSION`、`COMPILE_CUSTOM_KERNELS`、`VLLM_ASCEND_ENABLE_NZ` 等）。这些变量如果散落在各模块用 `os.getenv` 直接读，会有三个问题：默认值重复、类型转换重复、难以统一文档化。

`envs.py` 的解法很优雅：把「变量名 → 一个返回值的 lambda」集中登记在一个 `env_variables` 字典里，再用 Python 的**模块级 `__getattr__`**（PEP 562）实现「按需读取 + 统一类型转换」。这个文件本身是 Adapted from 上游 vLLM 的 `vllm/envs.py`。

#### 4.4.2 核心流程：惰性求值

普通做法是在 import 时就把所有环境变量求值存好；`envs.py` 不这样做，而是「声明 lambda，访问时才执行」：

```text
# 声明（import 时不执行 lambda 体）
env_variables = {
    "SOC_VERSION":         lambda: os.getenv("SOC_VERSION", None),
    "COMPILE_CUSTOM_KERNELS": lambda: bool(int(os.getenv("COMPILE_CUSTOM_KERNELS", "1"))),
    ...
}

# 访问（真正执行 lambda）
ascend_envs.COMPILE_CUSTOM_KERNELS   # 触发 __getattr__("COMPILE_CUSTOM_KERNELS")
        │
        └── name in env_variables? ──► 是: return env_variables[name]()  # 执行 lambda
                                       否: raise AttributeError
```

好处：

- **惰性**：用不到的变量永远不会去读环境、不会触发类型转换。
- **集中**：默认值与类型转换（如 `bool(int(...))`）只写一次。
- **可文档化**：文件里有一对 `begin-env-vars-definition` / `end-env-vars-definition` 标记注释，文档生成器会扫描这段区间自动生成环境变量文档。

#### 4.4.3 源码精读

`env_variables` 字典登记了所有变量，每个值是一个无参 lambda：

[vllm_ascend/envs.py:30-43](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/envs.py#L30-L43) — 字典开头是构建期变量（`MAX_JOBS`、`CMAKE_BUILD_TYPE`、`COMPILE_CUSTOM_KERNELS` 等），注意 `COMPILE_CUSTOM_KERNELS` 用 `bool(int(...))` 把字符串 `"0"/"1"` 转成布尔。

运行期变量里，有几个正在迁移到 `additional_config`、并标注了 `DEPRECATED`：

[vllm_ascend/envs.py:69-95](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/envs.py#L69-L95) — `VLLM_ASCEND_ENABLE_FLASHCOMM1`、`VLLM_ASCEND_ENABLE_NZ`、`VLLM_ASCEND_ENABLE_FUSED_MC2`、`VLLM_ASCEND_BALANCE_SCHEDULING` 等，注释里写明对应的新 `additional_config` key。

真正实现「惰性求值」的是模块级 `__getattr__`：

[vllm_ascend/envs.py:108-112](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/envs.py#L108-L112) — 当外部写 `from vllm_ascend import envs as ascend_envs; ascend_envs.SOC_VERSION` 时，触发 `__getattr__("SOC_VERSION")`，执行对应 lambda 返回值。

`__dir__` 则让 `dir(envs)` 能列出所有变量名，方便补全和文档：

[vllm_ascend/envs.py:115-116](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/envs.py#L115-L116) — 返回 `env_variables` 的所有 key。

文件顶部和底部还有文档生成器用的标记（在 u1-l3 已提及构建相关变量，这里聚焦其「文档化」作用）：

[vllm_ascend/envs.py:28](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/envs.py#L28) 与 [vllm_ascend/envs.py:105](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/envs.py#L105) — `# begin-env-vars-definition` 与 `# end-env-vars-definition` 之间的注释会被文档工具抽取，自动生成 `docs/.../env_vars.md`。

把 4.4 与 4.2 串起来看：`AscendConfig.__init__` 里 `ascend_envs.VLLM_ASCEND_BALANCE_SCHEDULING`（第 56 行）正是先走 `envs.py` 的 `__getattr__` 惰性求值，再把结果作为「默认值 / 兜底值」交给 `SchedulerConfig`——这就是两个文件的衔接点。

#### 4.4.4 代码实践

**实践目标**：亲手验证「模块级 `__getattr__` 的惰性求值」行为（无需 NPU，纯 Python）。

1. 写一段最小脚本（**示例代码**，非项目原有）：

```python
# 示例代码：演示 envs.py 的惰性求值
import os
from vllm_ascend import envs as ascend_envs

# 情况 A：不设环境变量，读默认值
print(ascend_envs.COMPILE_CUSTOM_KERNELS)   # 预期 True（默认 "1"）

# 情况 B：设环境变量后再读
os.environ["COMPILE_CUSTOM_KERNELS"] = "0"
print(ascend_envs.COMPILE_CUSTOM_KERNELS)   # 预期 False（每次访问都重新求值）
```

2. 观察现象：由于 `__getattr__` 每次都执行 lambda，所以**修改 `os.environ` 后立刻生效**——这正是「惰性」相对「import 时求值」的优势。
3. **预期结果**：第一次打印 `True`，设置环境变量后第二次打印 `False`。如果运行报 `ImportError`（缺少 `vllm`/`torch_npu` 依赖），则标注「待本地验证」，转而直接阅读 [envs.py:108-112](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/envs.py#L108-L112) 理解机制。

#### 4.4.5 小练习与答案

**练习 1**：如果访问 `ascend_envs.NOT_A_REAL_VAR`（字典里没有的名字），会发生什么？

**参考答案**：`__getattr__` 发现 `name not in env_variables`，执行 `raise AttributeError`，提示该模块没有这个属性。这与「普通模块访问不存在的名字」行为一致。

**练习 2**：为什么 `COMPILE_CUSTOM_KERNELS` 写成 `lambda: bool(int(os.getenv(..., "1")))`，而不是直接 `lambda: os.getenv(..., "1")`？

**参考答案**：因为环境变量永远是字符串。直接 `os.getenv` 拿到的是 `"1"` / `"0"` 这样的字符串，在 `if envs.COMPILE_CUSTOM_KERNELS:` 判断里永远为真（非空字符串都为真），语义错误。`int(...)` 再 `bool(...)` 才能把 `"0"` 正确转成 `False`。

---

## 5. 综合实践

把本讲内容串起来，完成一个「从用户 JSON 到运行期读取」的完整追踪任务：

**任务**：假设你要给一个 MoE 模型同时开启「平衡调度」「norm-quant 融合」「FlashComm1」三项，请完成以下步骤。

1. **写配置**：写出对应的 `additional_config` JSON，要求三项分别落在正确的子配置 / 顶层 key 里。提示：平衡调度走 `scheduler_config.enable_balance_scheduling`，norm-quant 融合走 `ascend_compilation_config.fuse_norm_quant`，FlashComm1 走顶层 `enable_flashcomm1`。

2. **画调用链**：画出从 `vllm serve ... --additional-config '...'` 到这三项被读取的链路，至少标注以下节点：
   - vLLM 把 JSON 存进 `VllmConfig.additional_config`；
   - `NPUPlatform.check_and_update_config`（[platform.py:408](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L408)）；
   - `init_ascend_config(vllm_config)`（[platform.py:431](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L431)）；
   - `AscendConfig.__init__` 内部三个读取点。

3. **回答优先级问题**：如果用户同时又 `export VLLM_ASCEND_ENABLE_FLASHCOMM1=0`，最终 FlashComm1 是开还是关？为什么？（提示：回看 4.2 的三级优先级与 [ascend_config.py:80-85](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py#L80-L85)。）

4. **验证（可选，需依赖）**：写一段脚本，用 `from vllm_ascend.ascend_config import AscendConfig` 构造一个最小 `AscendConfig`，打印三个属性值。若环境缺 `vllm`/`torch_npu`，则标注「待本地验证」并改读 [tests/ut/test_ascend_config.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/tests/ut/test_ascend_config.py) 中构造 `AscendConfig` 的辅助函数来理解。

**参考要点**：第 3 问答案是「开（True）」——因为 `additional_config.enable_flashcomm1=true` 是第一优先级，环境变量只在 `additional_config` 没给时才兜底。

## 6. 本讲小结

- `additional_config` 是 vLLM 给硬件插件预留的「自由配置 dict」，vLLM 不解释它，由 `AscendConfig` 负责解析。
- `AscendConfig.__init__` 用 `.get(key, {})` 把 JSON 切成若干子 dict，分别喂给 `AscendCompilationConfig` / `EplbConfig` / `SchedulerConfig` / `FinegrainedTPConfig` 等子配置类。
- 配置值遵循「三级优先级」：`additional_config` 显式值 → 环境变量（带 deprecation 警告）→ 默认值，由 `_get_config_value` 统一实现。
- `AscendConfig` 用模块级 `_ASCEND_CONFIG` 做单例缓存，`init`（带 `refresh`）/ `get`（只读）/ `clear`（清空）三函数管理生命周期；它在 `NPUPlatform.check_and_update_config` 第 431 行被构造。
- `envs.py` 用 `env_variables` 字典（名 → lambda）+ 模块级 `__getattr__` 实现「按需读取、统一类型转换」的惰性求值，并用 `begin/end-env-vars-definition` 标记支撑文档自动生成。
- 项目正在把一批 `VLLM_ASCEND_*` 环境变量迁移到 `additional_config`，过渡期两者都支持、未来只保留后者。

## 7. 下一步学习建议

- 下一讲 [u2-l3 前向上下文与 MoE 通信类型](u2-l3-forward-context-and-moe-comm.md) 会进入运行期：讲解 `ascend_forward_context.py` 如何在算子之间传递运行期信息，与 MoE 通信方式选择相关。
- 想深入某类子配置，建议直接读对应类：MoE 负载均衡读 `EplbConfig`（本讲 4.2）+ `vllm_ascend/eplb/`；图融合读 `AscendCompilationConfig` + `vllm_ascend/compilation/`（对应 u8 单元）。
- 想理解构建期环境变量（`SOC_VERSION` / `COMPILE_CUSTOM_KERNELS`）如何影响编译，回看 [u1-l3 环境准备与安装构建](u1-l3-build-and-install.md) 的 setup.py 流程。
