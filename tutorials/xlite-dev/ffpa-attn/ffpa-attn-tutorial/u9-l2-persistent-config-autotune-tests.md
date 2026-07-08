# 持久化配置与自动调优模式测试

## 1. 本讲目标

本讲聚焦两个测试文件——`tests/test_persistent_autotune_config.py` 与 `tests/test_triton_autotune_mode.py`——它们是 FFPA「自动调优与持久化配置」这条链路的**回归保护网**。学完后你应当掌握：

- 理解持久化配置测试的 **schema（数据契约）** 与**匹配断言**：如何用临时 config 目录 + monkeypatch 设备名，精确驱动 `lookup_persistent_config` 并断言其选中、缓存、回退、就近匹配的行为。
- 理解自动调优模式测试如何锁定 **fast/max 一致性**：候选生成数量、`autotune_mode` 合法值、seqlen 分桶、TMA/WS/persist 等开关的硬约束。
- 理解这些测试在测试体系与 CI 中的**角色**：它们是 CPU 安全的纯 Python 逻辑测试（不跑真实 kernel），专门守护调度/查找/序列化这些「不依赖 GPU」的代码路径。

> 承接：本讲依赖 [u8-l3 运行时配置查找与就近匹配回退](u8-l3-runtime-config-lookup-fallback.md)。本讲不重复讲解查找算法本身，而是讲**测试如何锁定这些行为**；生成端逻辑承接 [u8-l2](u8-l2-persistent-autotune-cli-generator.md)。

---

## 2. 前置知识

阅读本讲前，先用通俗语言建立三个概念：

- **持久化配置（persistent config）**：把「在某张显卡上对某形状调出的最优 launch config」写成一张设备专属 JSON（如 `NVIDIA_L20.json`），运行时零调优直接复用，避免每次启动都做耗时的在线 autotune。这张 JSON 由 `FFPA_TUNED_CONFIG_DIR` 环境变量指向。
- **monkeypatch（猴子补丁测试）**：pytest 提供的一种能力，可以在测试运行期间临时替换某个对象的属性或函数，测试结束后自动还原。本讲大量用它伪造 `torch.cuda.get_device_name` 等设备相关函数，从而**在没有 GPU 的机器上**也能测设备相关逻辑。
- **`tmp_path`**：pytest 的内置 fixture，每个测试函数自动获得一个**唯一的临时目录**，测试结束自动清理。本讲用它隔离每个测试的 config 目录，互不污染。

关键结论（来自 u8-l3）：`lookup_persistent_config` 接收一个 `PersistentConfigRequest`，先按 `direction/kernel/causal/dtype/has_attn_bias/has_dropout/enable_tma/enable_ws` 等**硬过滤**，再用 `nearest_value`（最近、并列取大）匹配 `head_dim`、用 `upper_or_max_value`（最小上界、否则取最大）匹配 `seqlen`；全程 `@lru_cache`，命中返回 config dict，未命中返回 `None`（**永不抛异常**，由上游回退默认 config）。

---

## 3. 本讲源码地图

本讲涉及的文件分两类：**被测源码**（左侧）与**测试文件**（右侧）。

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [src/ffpa_attn/triton/_persistent_autotune.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py) | 运行时配置查找核心 | `lookup_persistent_config`、过滤、就近匹配、dtype/arch 回退、环境变量 |
| [src/ffpa_attn/triton/_autotune_utils.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_autotune_utils.py) | seqlen 分桶 | `bucket_autotune_seqlen`、`autotune_seqlen_key`、`exact_autotune_seqlen_keys` |
| [src/ffpa_attn/functional.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py) | Backend 配置类 | `TritonBackend` 的 `autotune_mode`/TMA/WS/persist 硬约束 |
| [src/ffpa_attn/triton/_ffpa_fwd.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py) | 前向 launcher | 候选生成 `_gen_fwd_autotune_configs`、launcher 调用查找 |
| [src/ffpa_attn/autotune.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py) | 离线生成器 CLI | `TuneTask`、任务网格、payload、`_tune_forward/_tune_backward` |
| [tests/test_persistent_autotune_config.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_persistent_autotune_config.py) | **测试**：查找/回退/匹配 | 本讲主角之一 |
| [tests/test_triton_autotune_mode.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_triton_autotune_mode.py) | **测试**：mode/管线/生成 | 本讲主角之二 |

---

## 4. 核心概念与源码讲解

本讲按五个最小模块组织。前两个模块围绕 `test_persistent_autotune_config.py`，后三个围绕 `test_triton_autotune_mode.py`。

### 4.1 持久化配置查找的测试方法：注入临时目录与断言

#### 4.1.1 概念说明

测试「配置查找」最关键的问题是：**查找逻辑依赖当前显卡名（决定读哪个 JSON 文件）和 config 目录（决定去哪找）**，而测试环境往往没有目标显卡、也不能污染真实目录。解决办法是「**控制输入、断言输出**」：

1. 用 `monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path))` 把查找目录**重定向**到一个临时空目录。
2. 用 `monkeypatch.setattr` 把 `torch.cuda.current_device` / `get_device_name` **伪造成** `0` / `"NVIDIA L20"`，让查找以为当前在 L20 上。
3. 用 `write_config_file(payload, path)` 往临时目录写入**精心构造**的 JSON entry 列表。
4. 构造 `PersistentConfigRequest`，调用 `lookup_persistent_config`，**断言**返回的 config dict 与预期逐字段相等。

这套「四件套」是 `test_persistent_autotune_config.py` 几乎每个用例的固定骨架。

#### 4.1.2 核心流程

```text
_patch_cuda_device(monkeypatch)        # 伪造 current_device=0、get_device_name→"NVIDIA L20"
  └─ monkeypatch.setenv(CONFIG_ENV_VAR, tmp_path)   # 重定向 config 目录
  └─ clear_config_cache()               # 清掉上次测试的缓存，保证隔离
  └─ write_config_file(_payload([...]), device_config_path(tmp_path,"NVIDIA L20"))
  └─ config = lookup_persistent_config(PersistentConfigRequest(...))
  └─ assert config == { ... }           # 逐字段断言
```

其中 `_payload` 把若干 entry 包成完整 JSON 文档（带 `schema_version` 与 `device_name`）。

#### 4.1.3 源码精读

测试侧的公共工具函数：

[tests/test_persistent_autotune_config.py:12-24](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_persistent_autotune_config.py#L12-L24) 定义 `_payload` 与 `_patch_cuda_device`——前者组装合法 JSON 文档，后者伪造设备身份。注意 `device_name` 必须与 entry 里写的 `device_name` 字段对应（虽然查找主要靠**文件名**而非字段，见 4.2）。

被测的 `lookup_persistent_config` 入口在运行时先做两件事：检查「跳过」环境变量、算缓存 key：

[src/ffpa_attn/triton/_persistent_autotune.py:735-765](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L735-L765) 中，`lookup_persistent_config` 先看 `FFPA_SKIP_PERSISIT_TUNED_CONFIG=1`（注意此拼写，源码如此）则直接返回 `None`；否则用 `_lookup_cache_key` 算出 `(env_dir, device_index, request)` 三元组交给 `@lru_cache` 的 `_lookup_persistent_config_cached`。

一个典型用例 `test_lookup_forward_uses_shape_grid_nearest`（**最值得精读**）：

[tests/test_persistent_autotune_config.py:41-104](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_persistent_autotune_config.py#L41-L104) 写入两条 entry（head_dim 320 与 512），再用 head_dim=384、seqlen_q=3000、seqlen_k=32768 查询。它同时验证了**两个就近匹配规则**：384 最近匹配到 320（|384−320|=64 < |384−512|=128），3000 用 `upper_or_max_value` 匹配到 4096，32768 超过所有候选取最大 8192，最终选中第一条 entry。断言 `config["BLOCK_M"] == 64` 等逐字段相等，把「就近匹配」从直觉变成了可执行契约。

#### 4.1.4 代码实践

**实践目标**：亲手复现「注入临时目录 → 查询 → 断言」流程，理解测试如何驱动查找。

**操作步骤**：

1. 进入仓库根目录，运行：
   ```bash
   pytest tests/test_persistent_autotune_config.py -q
   ```
2. 阅读用例 `test_lookup_forward_uses_shape_grid_nearest`（第 41–104 行），把两条 entry 的 `(headdim, seqlen_q, seqlen_k)` 与请求的 `(384, 3000, 32768)` 列成对照表，手算每一步匹配结果。
3. 用 Python 交互式验证就近匹配：
   ```python
   from ffpa_attn.triton._persistent_autotune import nearest_value, upper_or_max_value
   assert nearest_value([320, 512], 384) == 320
   assert upper_or_max_value([4096, 2048], 3000) == 4096
   ```

**需要观察的现象**：所有用例通过；`pytest -q` 输出形如 `N passed in Xs`，无 GPU 也能跑。

**预期结果**：理解「写什么 entry、查什么 request、得什么 config」三者如何通过过滤 + 就近匹配串起来。

> 说明：以上命令与代码基于真实源码；若你当前环境未安装 `ffpa_attn`，可先 `pip install -e . --no-build-isolation`（Triton-only），步骤 3 不需要 GPU。

#### 4.1.5 小练习与答案

**练习 1**：`test_lookup_forward_uses_shape_grid_nearest` 中请求 head_dim=384，为什么最终选中了 320 而非 512 的 entry？

**参考答案**：`nearest_value` 以 `(abs(v−t), −v)` 排序，|384−320|=64 < |384−512|=128，320 更近；且 384 在二者正中间时也无并列，故选 320。

**练习 2**：为什么每个用例开头都要调用 `persistent.clear_config_cache()`？

**参考答案**：`load_config_entries` 与 `_lookup_persistent_config_cached` 都有进程级缓存。若不清，上一个用例写入的临时 entry 会「漏」到下一个用例，破坏隔离；`clear_config_cache` 同时清三个缓存（`_CONFIG_CACHE`、`_ARCH_CONFIG_CACHE`、`_DEVICE_NAME_CACHE`，并 `cache_clear` 掉 lru_cache）。

---

### 4.2 schema、dtype/arch 回退与就近匹配的测试覆盖

#### 4.2.1 概念说明

查找并非只有「命中」一种结果，测试必须覆盖全部退化路径：

- **schema 不匹配**：JSON 的 `schema_version` 不是 `SCHEMA_VERSION(=1)` → 返回空列表 → 查询返回 `None`。
- **dtype 回退（单向）**：fp16 查询可回退到 bf16 entry，但**反之不行**（bf16 不回退 fp16）；两者都有时优先精确匹配。
- **arch 回退**：当目录里**没有**当前设备名对应的文件时，扫描所有 JSON，用 `compute_capability` 字段匹配同架构文件；设备专属文件优先于 arch 回退；arch 不匹配则不回退。
- **就近匹配数学**：`nearest_value` 与 `upper_or_max_value` 各有明确的取舍规则。

#### 4.2.2 核心流程与数学

`nearest_value` 的并列取大规则可形式化为：

\[
\mathrm{nearest}(V, t) = \arg\min_{v \in V} \big( |v - t|,\; -v \big)
\]

即先按距离升序，距离相同时取较大的 `v`（因为 `−v` 更小）。这与「高侧桶偏好」一致。

`upper_or_max_value` 则是：

\[
\mathrm{upper\_or\_max}(V, t) =
\begin{cases}
\min\{v \in V \mid v \ge t\}, & \exists\, v \ge t \\
\max V, & \text{otherwise}
\end{cases}
\]

即「最小的上界，没有上界就取最大值」。

#### 4.2.3 源码精读

**就近匹配与设备名清洗**的纯函数测试，直接断言数学行为：

[tests/test_persistent_autotune_config.py:27-38](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_persistent_autotune_config.py#L27-L38) 覆盖 `sanitize_device_name`（`"NVIDIA L20"→"NVIDIA_L20"`、去掉首尾空格）与 `nearest_value`/`upper_or_max_value` 的典型取值（含 900 这种超出候选集的取最大）。对应实现 [src/ffpa_attn/triton/_persistent_autotune.py:308-336](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L308-L336)。

**schema 守门**测试：

[tests/test_persistent_autotune_config.py:1262-1280](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_persistent_autotune_config.py#L1262-L1280) 写入 `schema_version: -1` 的损坏文档，断言查询返回 `None`。对应 `load_config_entries` 的版本校验 [src/ffpa_attn/triton/_persistent_autotune.py:433](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L433)：`int(payload.get("schema_version", -1)) != SCHEMA_VERSION` 即视为空。

**dtype 单向回退**三个用例构成一组完整覆盖：

- [tests/test_persistent_autotune_config.py:1294-1353](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_persistent_autotune_config.py#L1294-L1353) `test_fp16_fallback_to_bf16_forward`：只有 bf16 entry，fp16 查询能命中（回退）。
- [tests/test_persistent_autotune_config.py:1356-1417](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_persistent_autotune_config.py#L1356-L1417) `test_fp16_prefers_exact_over_fallback`：fp16 与 bf16 都有，fp16 查询**优先精确**选 fp16 entry（`BLOCK_M=128`），而非回退的 bf16（`BLOCK_M=64`）。
- [tests/test_persistent_autotune_config.py:1420-1462](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_persistent_autotune_config.py#L1420-L1462) `test_bf16_does_not_fallback_to_fp16`：只有 fp16 entry，bf16 查询**不回退**，返回 `None`。

对应实现是查找里的「双候选桶」逻辑 [src/ffpa_attn/triton/_persistent_autotune.py:611-617](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L611-L617)：精确命中进 `exact_candidates`，仅当请求是 fp16 且 entry 是 bf16 才进 `fallback_candidates`，最后 `exact_candidates if exact_candidates else fallback_candidates`（[第 689 行](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L689)）。这就严格锁定了「fp16→bf16 单向、精确优先」的语义。

> 反向（bf16→fp16）不可回退的原因：bf16 与 fp16 的数值特性不同（bf16 精度低但范围大），把为 fp16 调出的 config 直接用于 bf16 不安全，故只允许更宽松的 bf16 兜底更严格的 fp16。

**arch 回退**三个用例：

- [tests/test_persistent_autotune_config.py:1549-1611](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_persistent_autotune_config.py#L1549-L1611)：设备名未知、但伪造 `compute_capability=8.9`，目录里有 `NVIDIA_L20.json`（字段标 `8.9`）→ arch 匹配，命中。
- [tests/test_persistent_autotune_config.py:1614-1686](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_persistent_autotune_config.py#L1614-L1686)：设备专属文件与 arch 文件同时存在且冲突，断言**设备专属优先**（`BLOCK_M=128` 而非 arch 的 64）。
- [tests/test_persistent_autotune_config.py:1689-1750](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_persistent_autotune_config.py#L1689-L1750)：arch 不匹配（设备 12.0、文件 8.9）→ 不回退，返回 `None`。

对应实现：设备专属加载 [src/ffpa_attn/triton/_persistent_autotune.py:599-602](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L599-L602) 先试 `load_config_entries`，空才退到 `_load_arch_config_entries`；后者扫描目录、比对 `compute_capability` 字段 [src/ffpa_attn/triton/_persistent_autotune.py:477-487](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L477-L487)。

#### 4.2.4 代码实践

**实践目标**：观察 dtype/arch 回退的边界条件。

**操作步骤**：

1. 运行 dtype 回退组与 arch 回退组：
   ```bash
   pytest tests/test_persistent_autotune_config.py -q -k "fp16 or arch or schema"
   ```
2. 阅读第 1294、1356、1420 三个用例，对照实现 611–617 行的双桶逻辑，画出「请求 dtype × entry dtype → 哪个桶」的真值表。

**需要观察的现象**：三组用例分别对应「回退成功」「精确优先」「回退失败」。

**预期结果**：你能用一句话讲清为何 bf16 不回退 fp16。

> 待本地验证：不同 torch 版本下 `torch.cuda.get_device_properties` 的伪造行为一致；本组用例均不触达 GPU。

#### 4.2.5 小练习与答案

**练习 1**：`test_arch_fallback_skips_wrong_arch` 中设备被伪造为 `major=12, minor=0`，为何查询返回 `None`？

**参考答案**：`_load_arch_config_entries` 用 `_get_compute_capability` 算出 `"12.0"`，与文件字段 `"8.9"` 不等（[第 482 行](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L482) `payload.get("compute_capability") != arch`），跳过该文件，无候选 → `None`。

**练习 2**：若一个 JSON 文档同时含 fp16 与 bf16 两条相同形状的 entry，fp16 查询会用到哪条？为什么？

**参考答案**：用 fp16 那条。因为精确命中先进 `exact_candidates`，最终 `candidates = exact_candidates`（非空时忽略 fallback），保证精度优先。

---

### 4.3 autotune mode 与 TritonBackend 配置管线的测试

> 从本模块起进入第二个测试文件 `test_triton_autotune_mode.py`。它的前半部分测的是 `functional.py` 的**配置管线**（而非 kernel），即「用户给的旋钮默认值是什么、非法组合如何被拒」。

#### 4.3.1 概念说明

`TritonBackend` 是 FFPA 默认后端的配置 dataclass，暴露 `autotune`、`autotune_mode`、`enable_tma`、`enable_ws`、`persist_dkdv`、`split_launch` 等旋钮。这些旋钮有**默认值**与**硬约束**：

- 默认 `autotune=False`、`autotune_mode="fast"`、所有 TMA/WS/persist 开关默认 `False`。
- `autotune_mode` 只能是 `"fast"` 或 `"max"`，否则断言失败。
- `persist_dkdv` 硬依赖 `enable_tma=True` 且 `backward=True`。
- 前向/反向的 TMA/WS 旋钮**可独立**设置（方向解耦）。

`FFPAAttnMeta.from_backends()` 用默认 Triton 后端构造 meta，是测试默认值的便捷入口。

#### 4.3.2 核心流程

```text
TritonBackend(...) ──__post_init__──> 断言 autotune_mode 合法、persist_dkdv 需 TMA
FFPAAttnMeta.from_backends(fwd, bwd) ──> 聚合成 forward_meta / backward_meta
测试断言 meta.<方向>_meta.<旋钮> == 期望默认值
```

#### 4.3.3 源码精读

**默认值锁定**：

[tests/test_triton_autotune_mode.py:46-57](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_triton_autotune_mode.py#L46-L57) 断言默认 `autotune_mode=="fast"`、`autotune is False`。[tests/test_triton_autotune_mode.py:69-79](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_triton_autotune_mode.py#L69-L79) 断言所有 TMA/WS/persist/split_launch 默认 `False`。对应字段声明 [src/ffpa_attn/functional.py:194-202](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L194-L202)。

**硬约束锁定**：

[tests/test_triton_autotune_mode.py:81-86](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_triton_autotune_mode.py#L81-L86) `test_persist_dkdv_requires_backward_tma` 用 `pytest.raises(AssertionError, match="persist_dkdv requires enable_tma")` 锁定：开 `persist_dkdv` 但不开 `enable_tma` 必抛断言。对应实现 [src/ffpa_attn/functional.py:214-216](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L214-L216)：`assert self.enable_tma, "persist_dkdv requires enable_tma=True"`。

[tests/test_triton_autotune_mode.py:185-187](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_triton_autotune_mode.py#L185-L187) 锁定 `autotune_mode="bad"` 被拒；对应 [src/ffpa_attn/functional.py:206-207](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L206-L207)。

**方向解耦**：

[tests/test_triton_autotune_mode.py:121-131](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_triton_autotune_mode.py#L121-L131) 给前向传 `enable_tma=True, enable_ws=True`、反向不开，断言前向开、反向关——锁定 TMA/WS 在两个方向上可独立设置。

#### 4.3.4 代码实践

**实践目标**：用最少的代码触发配置管线的默认值与硬约束。

**操作步骤**：

```bash
pytest tests/test_triton_autotune_mode.py -q -k "defaults or persist_dkdv or rejects"
```

阅读上述三个用例后，在 Python 中尝试（不需 GPU）：

```python
from ffpa_attn.functional import FFPAAttnMeta, TritonBackend
m = FFPAAttnMeta.from_backends()
print(m.backward_meta.autotune_mode)   # 'fast'
try:
    TritonBackend(forward=True, autotune_mode="bad")
except AssertionError as e:
    print("rejected:", e)
```

**预期结果**：打印 `fast` 与 `rejected: ...autotune_mode...`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `persist_dkdv` 必须依赖 `enable_tma`？

**参考答案**：`persist_dkdv` 把 dK/dV 累加器以 fp32 放在寄存器里跨 Q 块累加，依赖 Hopper TMA 的 `TensorDescriptor` 异步加载来掩盖显存延迟；没有 TMA，持久化路径无法高效运行（详见 [u5-l4](u5-l4-bwd-advanced-tma-ws-persist.md)）。故用 assert 在配置层 fail-fast。

**练习 2**：`split_launch` 是否依赖 `enable_tma`？测试如何确认？

**参考答案**：不依赖。[test_split_launch_accepts_without_backward_tma](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_triton_autotune_mode.py#L101-L107) 显示 `split_launch=True` 且 `enable_tma` 保持 `False` 能正常构造。

---

### 4.4 autotune 候选生成与 seqlen 分桶的测试

#### 4.4.1 概念说明

在线 autotune 的两个核心问题——**生成多少候选 config** 与**用什么 key 缓存**——都必须有测试锁定，否则一次重构就可能静默改变搜索空间或缓存命中率：

- **候选数量**：`fast` 模式候选少（搜索快）、`max` 模式候选多（搜索全）。测试断言 `len(fast) < len(max)`，并锁定 fast 模式下某些维度被「剪枝」后的具体取值集合。
- **seqlen 分桶**：相近序列长度应复用同一次调优。`bucket_autotune_seqlen` 把任意长度映射到桶上界；离线生成器则需要「精确 key」让每个目标形状各自被 benchmark，由 `exact_autotune_seqlen_keys()` 上下文管理器切换。

#### 4.4.2 核心流程

```text
_gen_fwd_autotune_configs(headdim, autotune_mode)  ──> list[triton.Config]   （数量随 mode 变）
bucket_autotune_seqlen(seqlen, mode)               ──> 桶上界 int
exact_autotune_seqlen_keys() 上下文内               ──> autotune_seqlen_key 返回原值（精确）
```

fast 模式下前向 generic 的候选数可在源码注释里直接读到：

[src/ffpa_attn/triton/_ffpa_fwd.py:147](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L147) 注释 `# fast: 2*1*2*2*1 = 8 configs; max: 2*2*2*2*2 = 32 configs`，即 fast=8、max=32（对 D=320/512 的大 D 路径）。

#### 4.4.3 源码精读

**候选剪枝锁定**：

[tests/test_triton_autotune_mode.py:214-221](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_triton_autotune_mode.py#L214-L221) `test_fwd_fast_mode_prunes_generic_configs` 断言三件事：`len(fast) < len(max)`；fast 下 `num_warps ∈ {4,8}`、`BLOCK_N == {64}`、`num_stages == {2}`——即 fast 把 `BLOCK_N` 与 `num_stages` 各剪到单值。对应生成 [src/ffpa_attn/triton/_ffpa_fwd.py:152-156](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L152-L156)：fast 时 `block_n` 只取 `[64]`、`num_stages` 只取 `[2]`。

**seqlen 分桶锁定**（参数化测试）：

[tests/test_triton_autotune_mode.py:190-203](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_triton_autotune_mode.py#L190-L203) 用 `@pytest.mark.parametrize` 列出 `(seqlen, expected_bucket)` 对：如 `(1,1024)`、`(1025,2048)`、`(8193,8192)`、`(16384,8192)`，锁定 fast 模式「1024 桶、8192 封顶」。对应实现 [src/ffpa_attn/triton/_autotune_utils.py:61-64](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_autotune_utils.py#L61-L64)。

**精确 key 切换锁定**：

[tests/test_triton_autotune_mode.py:206-211](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_triton_autotune_mode.py#L206-L211) `test_autotune_seqlen_key_uses_exact_context`：上下文外 `autotune_seqlen_key(513,"fast")==1024`（桶），上下文内 `==513`（精确），退出又恢复 `1024`。对应 [src/ffpa_attn/triton/_autotune_utils.py:93-97](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_autotune_utils.py#L93-L97)：`_EXACT_AUTOTUNE_SEQLEN_KEYS` ContextVar 为真时直接返回原值。

**反向候选**也有对称测试，如 [tests/test_triton_autotune_mode.py:592-605](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_triton_autotune_mode.py#L592-L605) 锁定 SM90 上 `max` 模式反向主 kernel 恰好 32 个候选、`BLOCK_HEADDIM ∈ {64,128}`、`num_stages ∈ {2,3}`。

#### 4.4.4 代码实践

**实践目标**：直接观察候选数量与桶值。

**操作步骤**（不需 GPU）：

```python
from ffpa_attn.triton._ffpa_fwd import _gen_fwd_autotune_configs
from ffpa_attn.triton._autotune_utils import bucket_autotune_seqlen
fast = _gen_fwd_autotune_configs(512, autotune_mode="fast")
mx   = _gen_fwd_autotune_configs(512, autotune_mode="max")
print(len(fast), len(mx))                 # 8 32
print(bucket_autotune_seqlen(1025, "fast"))  # 2048
```

**预期结果**：`8 32` 与 `2048`，与源码注释及测试断言一致。

#### 4.4.5 小练习与答案

**练习 1**：为何 `bucket_autotune_seqlen(16384, "fast")` 返回 `8192` 而非 `16384`？

**参考答案**：fast 模式有 8192 封顶（`_AUTOTUNE_SEQLEN_BUCKET_CAP`），超过 8192 一律映射到 8192（[第 62-63 行](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_autotune_utils.py#L62-L63)），让超长序列复用同一个调优结果。

**练习 2**：离线生成器为什么需要 `exact_autotune_seqlen_keys()`？

**参考答案**：离线生成器要让**目标网格里的每个形状各自被 benchmark 并落盘**（否则桶内多个形状只会调出一个代表），故用上下文切换到「精确 key」；运行时则用桶 key 以复用。详见 [u8-l2](u8-l2-persistent-autotune-cli-generator.md)。

---

### 4.5 离线生成器（payload / CLI flags / 任务网格 / entry 记录）的测试

#### 4.5.1 概念说明

`test_triton_autotune_mode.py` 的后半部分测的是**离线生成器** `src/ffpa_attn/autotune.py`（即 `python -m ffpa_attn.autotune` CLI）。生成器把「调优结果」写成 JSON entry，其字段必须与运行时查找的过滤字段**完全对齐**，否则写出来的 config 运行时查不到。测试分四块：

- **payload 的 `hardware_desc`**：记录 TMA/WS/split_launch 的方向化硬件描述，且字段名不能与旧字段冲突。
- **CLI 方向化开关**：旧的 `--enable-tma/--enable-ws` 应**同时**作用到前后向；新的方向化开关 `--enable-fwd-tma` 等不交叉启用。
- **任务网格**：`_iter_forward_tasks`/`_iter_backward_tasks` 产出的任务顺序、decode 特例、full-tasks 变体。
- **entry 记录**：`_tune_forward`/`_tune_backward` 在被 mock 的环境下产出的 entry，其 `kernel`、`enable_tma`、`enable_ws`、`config` 字段是否正确——尤其 SM90 TMA 路径要单独记录。

#### 4.5.2 核心流程

```text
_resolve_directional_cli_flags(args)   ──> 把 legacy 开关映射到 fwd/bwd 两套
_iter_forward_tasks(...)               ──> 产出 TuneTask 序列（common → decode → full 变体）
_tune_forward(task, ...) [被 mock]      ──> 产出 (entry, choices_count) 序列
_build_payload(entries, ...)           ──> 套上 schema_version/hardware_desc，写盘
```

#### 4.5.3 源码精读

**payload hardware_desc**：

[tests/test_triton_autotune_mode.py:327-367](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_triton_autotune_mode.py#L327-L367) 断言 payload 的 `hardware_desc` 含 `enable_forward_tma`/`enable_backward_tma`/`enable_forward_ws`/`enable_backward_ws`/`enable_backward_split_launch` 五个键，且**不含**旧的 `enable_tma`/`enable_ws`/`generation_options`——锁定字段命名迁移不回退。对应 `_build_payload` [src/ffpa_attn/autotune.py:833](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L833)。

**CLI 方向化开关**：

[tests/test_triton_autotune_mode.py:370-422](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_triton_autotune_mode.py#L370-L422) 三个用例分别锁定：legacy `enable_tma=True` 同时打开 fwd/bwd；方向化开关不交叉；`enable_bwd_split_launch=True` 允许在不打开 backward tma 时生效。对应 `_resolve_directional_cli_flags` [src/ffpa_attn/autotune.py:117](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L117)。

**任务网格**：

[tests/test_triton_autotune_mode.py:1132-1150](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_triton_autotune_mode.py#L1132-L1150) 锁定 decode 任务跳过 `Nkv==1` 与 `Nq==4` 的形状、保留 `Nq==1 且 Nkv>1`。[tests/test_triton_autotune_mode.py:1153-1217](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_triton_autotune_mode.py#L1153-L1217) 锁定任务以 `common`（prefill 基线）打头、full-tasks 追加 `attn-mask/dropout/gqa/mqa` 变体。对应 `_iter_forward_tasks` [src/ffpa_attn/autotune.py:237](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L237) 与 `_iter_backward_tasks` [第 292 行](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L292)。

**entry 记录（SM90 TMA）**：

[tests/test_triton_autotune_mode.py:425-499](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_triton_autotune_mode.py#L425-L499) 是本模块最值得精读的用例。它 monkeypatch 掉 `_make_tensors`、`ffpa_attn_func`、`_get_fwd_autotune`/`_get_fwd_sm90_autotune` 等，让 `_tune_forward` 在**不跑 kernel** 的情况下产出 entry 序列，断言：产出的 `kernel` 列表恰为 `["fwd_generic", "fwd_sm90_generic"]`；generic entry 的 `enable_tma/enable_ws` 都是 `False`（因为是 SM80 通用路径），sm90 entry 才是 `True`；sm90 entry 的 `config["warp_specialize"] is True`；且实际调用 `ffpa_attn_func` 时传入的 `forward_backend.enable_tma/enable_ws` 确为 `True`。对应 `_tune_forward` [src/ffpa_attn/autotune.py:500](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L500)。

这个用例揭示了**生成端与查找端的对齐契约**：生成器必须为 SM90 TMA kernel 写入 `enable_tma=True`、`enable_ws=True`，运行时查找（4.1）才能在 `request.enable_tma=True` 时命中它，否则 TMA 路径会查不到 config。

#### 4.5.4 代码实践

**实践目标**：跑通这批生成器单测，理解「mock 化」测试如何不依赖 GPU。

**操作步骤**：

```bash
pytest tests/test_triton_autotune_mode.py -q -k "payload or cli or tasks or records"
```

阅读 `test_persistent_tune_forward_records_sm90_tma_config`（第 425–499 行），列出它 monkeypatch 了哪些函数、为什么这些 patch 让测试**无需 GPU**。

**需要观察的现象**：用例通过；被 patch 的 `fake_ffpa_attn_func` 捕获到 `forward_backend.enable_tma is True`。

**预期结果**：你能解释「生成端写 `enable_tma=True`、运行时按 `enable_tma=True` 过滤」如何闭环。

#### 4.5.5 小练习与答案

**练习 1**：`test_persistent_tune_forward_records_sm90_tma_config` 为何要 monkeypatch `is_sm90_tma_forward_supported` 为 `True`？

**参考答案**：`_tune_forward` 只在该函数返回真时才走 SM90 TMA 分支并产出 `fwd_sm90_generic` entry。伪造它为真，可在任意 CPU 环境下触发 SM90 路径的 entry 记录逻辑，无需真实 Hopper GPU。

**练习 2**：若生成器漏写 SM90 entry 的 `enable_tma=True`，运行时会发生什么？

**参考答案**：运行时 `request.enable_tma=True` 会因 `_entry_flag_matches` 不匹配而过滤掉该 entry（[第 668-671 行](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L668-L671)），SM90 TMA 查询返回 `None`，launcher 回退到写死的默认 config，等于「调优白做了」。这正是本组测试要防的回归。

---

## 5. 综合实践

把本讲知识串起来，完成一个「**手写一条端到端断言**」的任务：

**任务**：在 `tests/test_persistent_autotune_config.py` 的风格下，写一段独立脚本（不修改仓库测试），完成以下闭环：

1. 用 `tmp_path` 建临时目录，设 `FFPA_TUNED_CONFIG_DIR` 指向它，伪造设备为 `"NVIDIA L20"`，`clear_config_cache()`。
2. 写入**两条** `fwd_generic` bf16 entry：一条 `(headdim=512, seqlen_q=2048, seqlen_k=8192, BLOCK_M=128)`，一条 `(headdim=320, seqlen_q=1024, seqlen_k=8192, BLOCK_M=64)`。
3. 用 `PersistentConfigRequest(direction="forward", kernel="fwd_generic", autotune_mode="fast", dtype="fp16", headdim=400, seqlen_q=3000, seqlen_k=9000, causal=False)` 查询。
4. 先**手算**每一步过滤与匹配，再跑脚本核对；若与预期不符，回到 4.1/4.2 的源码定位差异。

**提示（请先独立推导再对照）**：

- **dtype**：两条 entry 都是 bf16、请求是 fp16 → 二者都进 `fallback_candidates`，最终 `candidates = fallback_candidates`，两条都参与后续匹配。
- **head_dim**：`nearest_value([320,512], 400)`，距离 |400−320|=80、|400−512|=112，**选 320**，于是只剩 entry B（BLOCK_M=64）。
- **seqlen_q**：幸存 entry B 的 `seqlen_q={1024}`，`upper_or_max` 找不到 ≥3000 的值，取最大 1024。
- **seqlen_k**：`{8192}` 对 9000 同样取最大 8192。
- **结论**：返回非 `None`（fp16 回退 bf16 成功），`BLOCK_M == 64`。

> 待本地验证：本任务全程不触达 GPU，纯调度逻辑。

---

## 6. 本讲小结

- **`test_persistent_autotune_config.py`** 用「monkeypatch 设备名 + `tmp_path` 重定向 config 目录 + `clear_config_cache` 隔离 + `write_config_file` 构造 entry」四件套，把运行时查找的「命中/缓存/回退/就近匹配」全部变成可执行断言。
- **schema 守门、dtype 单向回退（fp16→bf16，精确优先）、arch 回退（设备专属优先、compute_capability 匹配）** 三类退化路径都有专门用例覆盖。
- **就近匹配**有严格数学：`nearest_value` 用 `(距离, −v)` 实现并列取大；`upper_or_max_value` 取最小上界或最大值。
- **`test_triton_autotune_mode.py`** 前半锁定 `TritonBackend` 的默认值与硬约束（`autotune_mode∈{fast,max}`、`persist_dkdv` 依赖 `enable_tma`、TMA/WS 方向解耦）。
- 后半锁定 **fast/max 候选数量与剪枝**、**seqlen 分桶与精确 key 切换**，以及**离线生成器的 payload/CLI/任务网格/entry 记录**，确保生成端写入的字段与运行时查找的过滤字段**端到端对齐**。
- 这两个文件是 **CPU 安全的纯 Python 逻辑测试**：通过 monkeypatch 与 mock，全程不跑真实 kernel，守护的是调度、查找、序列化、配置管线这些不依赖 GPU 的代码路径。

---

## 7. 下一步学习建议

- 想看这些被测逻辑在真实运行时如何被调用，回到 [u8-l3 运行时配置查找与就近匹配回退](u8-l3-runtime-config-lookup-fallback.md)，对照 launcher 的调用点 [src/ffpa_attn/triton/_ffpa_fwd.py:959-983](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L959-L983)。
- 想理解离线生成器的完整 CLI 与多卡并行，继续 [u8-l2 持久化调优配置生成器 CLI](u8-l2-persistent-autotune-cli-generator.md) 与 [u8-l4 Ray 多 GPU 并行调优](u8-l4-ray-multi-gpu-autotune.md)。
- 若要扩展测试覆盖（如新增 head_dim/kernel），参考 [u9-l4 二次开发扩展指南](u9-l4-extension-guide.md)，并在 `_KERNEL_CONFIG_KEYS`（[src/ffpa_attn/triton/_persistent_autotune.py:36-163](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L36-L163)）与对应测试中同步登记。
