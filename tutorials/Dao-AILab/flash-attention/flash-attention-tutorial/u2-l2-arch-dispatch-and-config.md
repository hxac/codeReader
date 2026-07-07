# 架构分发与 tile 配置选择

## 1. 本讲目标

在 [u2-l1](u2-l1-public-api.md) 里我们已经看到：公共 API `flash_attn_func` 只是一层薄包装，真正的活儿在 `_flash_attn_fwd` 里。本讲要回答一个紧接着的问题——

> 同一个 `flash_attn_func(q, k, v, causal=True)` 调用，在 Ampere（SM80）、Hopper（SM90）、Blackwell（SM100/110）上跑的其实是**五个完全不同的 kernel 类**。FA4 是怎么决定用哪一个的？决定之后，每个 kernel 又是怎么挑出自己那套 `tile_m`/`tile_n`/`num_threads` 的？

学完本讲你应该能够：

1. 说清楚 `_get_device_arch()` 如何探测 GPU 架构，以及 `FLASH_ATTENTION_ARCH` 环境变量如何强行覆盖它。
2. 理解头维（`head_dim` / `head_dim_v`）的合法性校验规则 `_validate_head_dims`，以及为什么不同架构允许的头维范围不同。
3. 看懂 `FwdConfig` 与 `_tile_size_fwd_sm90` 如何依据架构 + 因果/局部 + 头维，自动选出 `tile_m`/`tile_n` 以及两个会影响 kernel 实现的布尔标志。
4. 解释「为什么架构分发只改变实现、不改变数学结果」。

本讲**不**讲 tile 调度（persistent/CLC）、2CTA、SplitKV 的内部机制——那些是后续专家层（u8、u7）的内容。本讲只聚焦「Python 层的分发与配置决策」这一个环节。

## 2. 前置知识

本讲默认你已经读过 [u2-l1](u2-l1-public-api.md)，知道：

- FA4 公共 API 是 `flash_attn_func` / `flash_attn_varlen_func`，内部经 `_flash_attn_fwd` 编译并调用 kernel。
- FA4 是**运行时 JIT 编译**的：第一次调用慢，之后命中 `compile_key` 缓存。
- `compile_key` 把架构、头维、tile 尺寸等做成一个元组当作缓存键。

此外补充三个本讲要用到的小概念：

- **compute capability（计算能力）**：NVIDIA 用 `major.minor` 描述 GPU 架构，例如 H100 是 `9.0`、B200 是 `10.0`、A100 是 `8.0`。FA4 内部把它压成一个整数 `major*10 + minor`，于是 9.0→90、10.0→100、8.0→80。下文写作 `arch`。
- **MMA 指令代际**：Ampere 用 `m16n8k16` 一类 warp 级 MMA；Hopper 引入 warp-group MMA（WGMMA）；Blackwell 引入基于 `tcgen05` 的 UMMA。这三代指令**互不兼容**，所以必须为每代 GPU 写一套 kernel。
- **SMEM（共享内存）/ occupancy（占用率）**：一个 thread block 能用的共享内存有上限（Ampere ~164KB、Hopper ~228KB、Blackwell 消费级 ~99KB）。tile 取得越大，单 block 占的 SMEM 越多，能并发执行的 block 数（occupancy）就越少——tile 选择本质上是在「单 block 计算密度」和「并发 block 数」之间找平衡。

## 3. 本讲源码地图

本讲几乎全部内容都在一个文件里：

| 文件 | 作用 |
| --- | --- |
| [`flash_attn/cute/interface.py`](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py) | 公共 API 与 `_flash_attn_fwd`。本讲的三个主角——架构探测、头维校验、tile 配置——全在这里。 |
| [`flash_attn/cute/utils.py`](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py) | `FA_CLC` / `FA_DISABLE_2CTA` 等环境变量的读取（影响分发时几个布尔标志的默认值）。 |

被分发的五个 kernel 类（只需知道它们的名字和所在文件，本讲不深入内部）：

- SM80：`FlashAttentionForwardSm80`（`flash_fwd.py`）
- SM90：`FlashAttentionForwardSm90`（`flash_fwd_sm90.py`）
- SM100/110：`FlashAttentionForwardSm100`（`flash_fwd_sm100.py`）；头维 256 时改用 `BlackwellFusedMultiHeadAttentionForward`（`sm100_hd256_2cta_fmha_forward.py`）
- SM120：`FlashAttentionForwardSm120`（`flash_fwd_sm120.py`）

## 4. 核心概念与源码讲解

### 4.1 架构探测：`_get_device_arch` 与 `FLASH_ATTENTION_ARCH`

#### 4.1.1 概念说明

FA4 是一套 Python 源码，但它要服务的 GPU 跨越三代架构。问题是：**当用户调用 `flash_attn_func` 时，FA4 怎么知道当前机器是什么 GPU，从而选对 kernel？**

答案是一个极小的函数 `_get_device_arch()`：它返回一个整数（如 80/90/100/120），`_flash_attn_fwd` 再用 `arch // 10` 把它压成「代号」（8/9/10/11/12）去做 if/elif 分发。这个函数还支持用环境变量 `FLASH_ATTENTION_ARCH` **强行覆盖**真实硬件——这是调试、测试和「在没有 GPU 的机器上编译 kernel」的关键开关。

注意两个层面的区分（这是本小节最容易被忽略的要点）：

- **kernel 选择（FA4 决定）**：`FLASH_ATTENTION_ARCH` 控制，决定用哪个 Python kernel 类。
- **编译目标（CuTeDSL 决定）**：`CUTE_DSL_ARCH` 控制，决定把 kernel 编译成哪个 PTX 架构。

两者通常是匹配的，但**可以解耦**：你可以选 SM90 的 kernel 类、却编译成 SM80 的 PTX（用于无 GPU 环境下的离线编译测试）。

#### 4.1.2 核心流程

```
flash_attn_func(q,k,v)
  └─> _flash_attn_fwd(...)
        arch = _get_device_arch()        # 探测/覆盖，得到整数 arch
        assert arch//10 in [8,9,10,11,12]
        ...
        if   arch//10 == 8:   用 FlashAttentionForwardSm80
        elif arch//10 == 9:   用 FlashAttentionForwardSm90
        elif arch//10 in [10,11]: 用 FlashAttentionForwardSm100 (或 hd256 专用)
        elif arch//10 == 12:  用 FlashAttentionForwardSm120
```

`_get_device_arch` 内部的决策只有两步：

1. 若设置了 `FLASH_ATTENTION_ARCH`，解析它（走 `_parse_arch_str`）。
2. 否则调用 `torch.cuda.get_device_capability()`，把 `(major, minor)` 压成 `major*10 + minor`。

#### 4.1.3 源码精读

先看字符串解析助手，它把 `'sm_80'`、`'sm_90a'`、`'80'`、`'100'` 这些写法统一成整数：

[flash_attn/cute/interface.py:66-73](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L66-L73) — 用正则把架构字符串里的 major/minor 抠出来，返回 `major*10 + minor`。末尾的 `[af]?` 兼容 `sm_90a`（a=accelerator，Hopper 的 TMA/WGMMA 特性变体）这类后缀，但后缀本身不影响整数编码。

再看主角 `_get_device_arch`：

[flash_attn/cute/interface.py:76-92](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L76-L92) — 三个要点：

1. `@lru_cache(maxsize=None)`：**结果在进程内被永久缓存**。这意味着 `FLASH_ATTENTION_ARCH` 必须在**第一次调用 `_get_device_arch()` 之前**设置好（通常就是进程启动时通过环境变量设置）。运行到一半再 `os.environ[...] = ...` 不会生效，因为缓存里已经存了旧值。
2. `arch_override` 分支：有覆盖就直接 `_parse_arch_str`，**完全不碰 CUDA**——这就是为什么「无 GPU 也能编译 kernel」。
3. 默认分支：`torch.cuda.get_device_capability()` 返回 `(major, minor)`，压成整数。

然后在 `_flash_attn_fwd` 里，arch 被取出来并断言：

[flash_attn/cute/interface.py:446-451](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L446-L451) — `_arch` 形参允许调用方直接传架构整数（测试用），否则现算 `arch = _get_device_arch()`。注意第 450 行有个细节：`_validate_head_dims` **只对 9/10/11 调用**，对 8 和 12 跳过（原因见 4.2）。

真正的大分发在编译分支里：

[flash_attn/cute/interface.py:823-965](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L823-L965) — 按 `arch // 10` 实例化不同的 kernel 类。把这一大段提炼成对照表：

| `arch // 10` | kernel 类 | 默认 `num_threads` | 该分支的关键限制（来自源码 assert） |
| --- | --- | --- | --- |
| 8（SM80） | `FlashAttentionForwardSm80` | 128 | 不支持 paged KV、不支持 SplitKV |
| 9（SM90） | `FlashAttentionForwardSm90` | 384 | 不支持 SplitKV |
| 10/11（SM100/110） | `FlashAttentionForwardSm100`（或 hd256 时 `BlackwellFusedMultiHeadAttentionForward`） | 384 | 功能最全：SplitKV、paged KV、2CTA、MLA |
| 12（SM120） | `FlashAttentionForwardSm120` | 128 | 不支持 block sparsity、不支持 paged KV、不支持 SplitKV |

> 为什么 SM80/SM120 是 128 线程（4 个 warp），而 SM90/SM100 是 384？因为 Hopper 的 WGMMA 和 Blackwell 的 UMMA 都是 **warp-group（4 个 warp=128 线程）** 为单位发射的，FA4 在这些架构上用 `num_wg+1` 个 warp-group；而 Ampere/SM120 用的是老式 warp 级 MMA，4 个 warp 就够了。这个赋值在 [interface.py:522-524](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L522-L524)。

最后，`arch` 本身会进 `compile_key`（[interface.py:750](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L750)），所以**切换架构会触发重新编译**——这一点在 u2-l1 已讲过，这里只点明 `arch` 是缓存键的一员。

#### 4.1.4 代码实践

**实践目标**：在不跑 GPU 的前提下，亲手验证架构探测机制与 `lru_cache` 行为。

**操作步骤**（纯 Python，任何机器都能跑）：

```python
# 示例代码：探查架构分发机制（不需要 GPU）
import os
os.environ["FLASH_ATTENTION_ARCH"] = "sm_90"   # 必须在 import 之前设？其实只要在首次调用前

from flash_attn.cute.interface import _get_device_arch, _parse_arch_str

# 1. 字符串解析：同一架构的多种写法应得到同一整数
for s in ["sm_80", "80", "SM_90a", "100", "sm_120"]:
    print(s, "->", _parse_arch_str(s))

# 2. _get_device_arch 走 env 覆盖分支，不碰 CUDA
print("arch =", _get_device_arch())   # 期望 90
print("cache info:", _get_device_arch.cache_info())  # 看到 hits=1, misses=1

# 3. 验证 lru_cache：现在改环境变量已经晚了
os.environ["FLASH_ATTENTION_ARCH"] = "sm_80"
print("arch after env change =", _get_device_arch())  # 仍然是 90，因为缓存
```

**需要观察的现象**：第 1 步四种 `sm_80`/`80` 写法都得到 80；第 2 步得到 90 且 `cache_info` 显示已缓存；第 3 步改环境变量后仍返回 90。

**预期结果**：证明 (a) 解析器对大小写/后缀兼容；(b) env 覆盖绕过 CUDA；(c) `lru_cache` 使「运行中改 env」失效。

> 待本地验证：若你的环境装了 CUDA 且没有设 `FLASH_ATTENTION_ARCH`，第 2 步会真的去查 `torch.cuda.get_device_capability()`，需要一张 NVIDIA GPU。

#### 4.1.5 小练习与答案

**练习 1**：用户在脚本第 10 行 `import flash_attn.cute`，第 20 行才 `os.environ["FLASH_ATTENTION_ARCH"]="sm_80"`，第 30 行调用 `flash_attn_func(...)`。架构覆盖会生效吗？

**答案**：大概率**不生效**。`import` 本身一般不触发 `_get_device_arch()`（它是在 `_flash_attn_fwd` 里才调用的），所以理论上第 30 行首次调用前设置是来得及的；但只要在第 30 行之前**有过任何一次**前向调用（哪怕在别的脚本里复用了同一进程），缓存就已定型。安全做法是**进程启动前**通过 shell 环境变量设置：`FLASH_ATTENTION_ARCH=sm_80 python your_script.py`。

**练习 2**：为什么 docstring（[interface.py:84-87](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L84-L87)）建议「无 GPU 编译时同时设 `FLASH_ATTENTION_ARCH` 和 `CUTE_DSL_ARCH`」？

**答案**：`FLASH_ATTENTION_ARCH` 只决定**选哪个 kernel 类**（Python 层），但 CuTeDSL 把这个类编译成 PTX 时还需要一个**编译目标架构**，那由 `CUTE_DSL_ARCH` 决定。无 GPU 时 `torch.cuda.get_device_capability()` 会报错，所以两者都得手动指定，才能让编译流水线完整跑通。

### 4.2 头维合法性校验：`_validate_head_dims`

#### 4.2.1 概念说明

不同架构的 kernel 支持的头维（`head_dim`，Q/K 的最后一维；`head_dim_v`，V 的最后一维）范围不一样。比如 SM90 的 kernel 写法支持到 256，而 SM100 的主 kernel 只支持到 128（外加几个特例形状）。

如果放任不支持的形状进到 JIT 编译阶段，用户会得到一个晦涩的编译错误，等上几十秒才知道。`_validate_head_dims` 的作用是**在昂贵的编译之前**用一行 `assert` 早失败、给出清晰报错。这是一个典型的「前置护栏（early-fail guard）」。

这里还有一个**对齐约束**：FA4 要求张量最后一维 16 字节对齐（详见 [u1-l3](u1-l3-install-and-first-run.md)）。对 fp16/bf16（每元素 2 字节），这意味着 `head_dim` 必须是 8 的倍数；校验里用 `head_dim % alignment == 0` 来强制这一点。

#### 4.2.2 核心流程

```
alignment = 16 // element_size        # fp16/bf16 → 8；fp32 → 4
# SM80/SM120 跳过校验；SM90/SM100/SM110 调用：
_validate_head_dims(head_dim, head_dim_v, arch//10, alignment)
   ├─ SM90：要求 8 ≤ head_dim, head_dim_v ≤ 256，且都整除 alignment
   └─ SM100/110：要求 (标准 8~128) 或 (DeepSeek 192,128) 或 (MLA absorbed * ,512) 或 (hd256 256,256)，
                  且都整除 alignment
```

#### 4.2.3 源码精读

先看对齐值是怎么算出来的：

[flash_attn/cute/interface.py:449](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L449) — `alignment = 16 // v.element_size()`。`element_size()` 返回每个元素的**字节数**，于是对齐阈值是「16 字节能装下几个元素」。fp16/bf16 是 2 字节 → 8；fp32 是 4 字节 → 4。

再看校验函数本体：

[flash_attn/cute/interface.py:95-112](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L95-L112) — 把它读成三段：

1. **特例形状**（第 97-100 行）预先识别：DeepSeek 的 `(192, 128)`、MLA absorbed 的 `(64, 512)` 或 `(==head_dim_v, 512)`、头维 256 的 `(256, 256)`，以及标准范围 `8 ≤ d, d_v ≤ 128`。
2. **SM90 分支**（第 102-107 行）：宽松地允许 `8 ≤ d, d_v ≤ 256`，只要整除 alignment。
3. **SM100/110 分支**（第 108-112 行）：必须是「标准范围」或四选一的特例形状，且整除 alignment。报错信息直接列出 DeepSeek、hd256 等合法特例，方便用户对号入座。

注意 [interface.py:450-451](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L450-L451) 的 `if arch // 10 not in [8, 12]:` —— **SM80 与 SM120 不调用这个校验**。原因有二：(a) 这两代 kernel 内部对头维有自己的处理，统一约束反而碍事；(b) 对齐要求仍由张量层的 16 字节假设兜底。所以 SM80/SM120 用户传非法头维时，会在更晚的阶段（编译或运行）才报错——这是有意的取舍。

#### 4.2.4 代码实践

**实践目标**：亲手喂几组头维给 `_validate_head_dims`，观察哪些被接受、哪些触发 AssertionError。

**操作步骤**（纯 Python，无 GPU）：

```python
# 示例代码：探查头维校验规则
from flash_attn.cute.interface import _validate_head_dims

alignment = 8  # 模拟 fp16
cases = [
    (64, 64, 9),    # SM90 标准
    (256, 256, 9),  # SM90 上限
    (192, 128, 10), # SM100 DeepSeek 形状
    (64, 512, 10),  # SM100 MLA absorbed
    (96, 96, 10),   # SM100 标准
    (192, 128, 9),  # SM90 也允许（8~256）
    (160, 160, 10), # SM100 非法：不在标准范围也不是特例
    (64, 64, 8),    # SM80：根本不进校验（调用方跳过）
]
for hd, hdv, cc in cases:
    try:
        _validate_head_dims(hd, hdv, cc, alignment)
        print(f"(hd={hd}, hdv={hdv}, cc={cc}) -> OK")
    except AssertionError as e:
        print(f"(hd={hd}, hdv={hdv}, cc={cc}) -> REJECTED")
```

**需要观察的现象**：前 6 组 OK；`(160,160,10)` 被拒（既不是 8~128 标准范围，也不是任何特例）；`(64,64,8)` 也 OK——但**注意**：SM80 在 `_flash_attn_fwd` 里根本不会调用本函数（见 4.2.3），这里直接调用只是为了演示函数本身的逻辑。

**预期结果**：SM100 对 `(160,160)` 报错，证明校验确实是「标准范围 ∪ 特例形状」的并集判断。

#### 4.2.5 小练习与答案

**练习 1**：fp16 下 `head_dim=72` 在 SM90 上能否通过校验？在 SM100 上呢？

**答案**：`72 % 8 == 0` 满足对齐。SM90：`8 ≤ 72 ≤ 256`，**通过**。SM100：72 不在特例集合里，且 `8 ≤ 72 ≤ 128` 属于标准范围，**也通过**。

**练习 2**：为什么 `_validate_head_dims` 要单独识别 `(192, 128)`、`(64, 512)` 这类「奇怪」的形状？

**答案**：这些不是任意取值，而是真实模型的结构——`(192,128)` 是 DeepSeek 的头维配置（Q/K 192，V 128），`(64,512)` 是 MLA absorbed 形式（详见 u10-l2）。SM100 专门为它们写了高效 kernel 路径，所以校验函数要把它们「白名单」放行，否则会被标准范围规则误拒。

### 4.3 tile 与线程配置：`FwdConfig` 与 `_tile_size_fwd_sm90`

#### 4.3.1 概念说明

选定了 kernel 类、确认了头维合法之后，还要回答一个问题：**这个 kernel 内部用多大的 tile？**

tile（分块）是 FlashAttention 的灵魂（见 [u1-l1](u1-l1-what-is-flashattention.md)）。前向 kernel 把 Q 切成 `tile_m` 行一块、把 K/V 切成 `tile_n` 行一块，在 SMEM 里做一次 `tile_m × tile_n` 的注意力。`tile_m`/`tile_n` 太小，MMA 利用率低；太大，SMEM 爆炸导致 occupancy 跌甚至编译失败。

`FwdConfig` 是一个 frozen dataclass，打包四个值：

- `m_block_size`：Q 分块的行数（即 `tile_m`）。
- `n_block_size`：K/V 分块的行数（即 `tile_n`）。
- `mma_pv_is_rs`：PV 矩阵乘的输入 P/V 是否放在**寄存器**（RS）而非共享内存。
- `intra_wg_overlap`：是否在 warp-group 内部做计算/访存重叠（OL = overlap）。

后两个布尔标志是 SM90 才用到的优化开关，它们会改变 kernel 的内部结构（因此也进 `compile_key`）。

#### 4.3.2 核心流程

`_flash_attn_fwd` 选 tile 的逻辑分三档（[interface.py:527-543](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L527-L543)）：

```
if 用户显式传了 tile_mn:
    fwd_cfg = FwdConfig(tile_mn[0], tile_mn[1], <默认标志>)     # 用户说了算
elif arch//10 == 12 (SM120):
    head_dim<=64 → FwdConfig(128,128,T,T)；否则 FwdConfig(128,64,T,T)
elif arch//10 == 8  (SM80):
    FwdConfig(128,64,T,T)
elif arch//10 == 9  (SM90):
    fwd_cfg = _tile_size_fwd_sm90(head_dim, head_dim_v, causal, local, sparse_q)
# SM100 的 tile 由其 kernel 类内部决定，这里走默认 FwdConfig(128,128,T,T)
```

SM90 的 `_tile_size_fwd_sm90` 是其中最讲究的——它依据 `head_dim` 分档，并因 `is_causal`/`is_local` 微调 `tile_n`：

| `head_dim` | 非因果/非局部 | 因果或局部 | 备注 |
| --- | --- | --- | --- |
| ≤ 64 | `(192, 128, RS, OL)` | 同 | 192×128 在各 seqlen 下都最优 |
| ≤ 96 | `(192, 144, noRS, OL)` | `(192, 128, noRS, OL)` | hdim=96 时 RS 会灾难性掉速，强制 noRS |
| ≤ 128 | `(128, 128, RS, OL)` | 同 | — |
| ≤ 192 | `(128, 128或112, RS, OL)` | `(128, 96, RS, OL)` | tile_n 随 head_dim_v 与 local 变化 |
| 256 | `(128, 80, RS, OL)` | `(128, 64, RS, OL)` | 大头维必须缩 tile_n |

> 为什么头维越大、`tile_n` 越小？粗略地，前向 SMEM 同时要装一块 Q（`tile_m × head_dim`）和若干级 K/V 块（`stages × tile_n × (head_dim + head_dim_v)`）。`head_dim` 翻倍时，为了让 SMEM 总量不爆，只能把 `tile_n` 减半。代码注释（[interface.py:529-531](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L529-L531)）直接给出了 SM120 的 SMEM 占用估算：`128×128 → 48KB`、`128×64 → 64KB`，依据是 99KB 的 SMEM 容量。

#### 4.3.3 源码精读

先看 `FwdConfig` 定义：

[flash_attn/cute/interface.py:115-120](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L115-L120) — 四个字段，`frozen=True` 表示不可变（可哈希，能安全地参与 `compile_key`）。

再看三档选择逻辑：

[flash_attn/cute/interface.py:526-547](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L526-L547) — 注意几个细节：(a) 第 526 行先给一个默认 `FwdConfig(128,128,True,True)`；(b) 用户传 `tile_mn` 时（第 541-542 行）只覆盖 `m/n`，保留默认的两个布尔标志；(c) `num_threads` 在 [522-524 行](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L522-L524) 已按 SM80/SM120 改成 128；(d) `mma_pv_is_rs` / `intra_wg_overlap` 若用户没指定，就取 `fwd_cfg` 里的值（第 544-547 行）。

SM90 的精细分档函数：

[flash_attn/cute/interface.py:123-155](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L123-L155) — 逐段读：

- **`head_dim <= 64`**（132-137 行）：基准 `192×128`。注释说明这是从 C++ FA3 的 `tile_size.h` 迁移过来的，但在 Python kernel 上 `192×128 RS+OL` 跨所有 seqlen 都最稳。`sparse_block_size_q` 是块稀疏场景的约束（tile_m 必须整除稀疏块大小，否则退化到 128）。
- **`head_dim <= 96`**（138-147 行）：注释里有条关键经验——「Python kernel 上 192× tile 配 RS 会灾难性掉速（~300 vs ~600 TFLOPS）」，所以**强制 `noRS+OL`**。这是用真实 H100 benchmark 换来的硬结论。
- **`head_dim <= 128` / `192` / `256`**（148-155 行）：随头维增大逐步把 `tile_n` 从 128→112→96→80→64 缩小，local 模式额外再缩一档。

最后，`tile_m`/`tile_n`/`num_threads`/`mma_pv_is_rs`/`intra_wg_overlap` 全部进 `compile_key`（[interface.py:744-755](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L744-L755)），所以**改 tile 或改架构都会触发重编译**。

顺带一提：分发阶段还有两个布尔标志由环境变量在 import 时读定，它们也影响 kernel 行为：

[flash_attn/cute/utils.py:66-99](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L66-L99) — `FA_CLC`（启用 CLC 动态调度）与 `FA_DISABLE_2CTA`（禁用 2CTA 指令）。注意它们是**模块级常量**，import 时求值一次。`_flash_attn_fwd` 在 [interface.py:517-518](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L517-L518) 取这两个默认值。这两个开关的内部机制属于 u8 的范畴，这里只需知道它们也是「分发阶段的配置输入」。

#### 4.3.4 代码实践

**实践目标**：用 Python 直接查询 `_tile_size_fwd_sm90`，验证不同头维/掩码组合下 tile 的变化，把本节的对照表亲手跑一遍。

**操作步骤**（纯 Python，无 GPU）：

```python
# 示例代码：查询 SM90 tile 配置表
from flash_attn.cute.interface import _tile_size_fwd_sm90

configs = [
    # (head_dim, head_dim_v, causal, local)
    (64, 64, False, False),
    (64, 64, True, False),
    (96, 96, False, False),
    (96, 96, True, False),    # 期望 tile_n 从 144 缩到 128
    (128, 128, False, False),
    (192, 128, False, False), # DeepSeek 形状
    (192, 128, True, False),
    (256, 256, False, False), # 期望 tile_n=80
    (256, 256, True, False),  # 期望 tile_n=64
]
for hd, hdv, c, l in configs:
    cfg = _tile_size_fwd_sm90(hd, hdv, c, l)
    print(f"hd={hd:>3} hdv={hdv:>3} causal={int(c)} local={int(l)} "
          f"-> m={cfg.m_block_size} n={cfg.n_block_size} "
          f"rs={int(cfg.mma_pv_is_rs)} ol={int(cfg.intra_wg_overlap)}")
```

**需要观察的现象**：`hd=96` 时 causal 把 `n` 从 144 降到 128；`hd=192` causal 时 `n=96`；`hd=256` 在非因果时 `n=80`、因果时 `n=64`；`hd=64` 全程 `192×128`。

**预期结果**：输出与 4.3.2 的对照表完全一致，证实 tile 选择是「头维分档 + 掩码微调」的确定性函数。

#### 4.3.5 小练习与答案

**练习 1**：用户调用 `flash_attn_func(q, k, v, causal=True)` 时没有传 `tile_mn`。在 SM90、`head_dim=128` 的情况下，最终用多大的 tile？

**答案**：`_tile_size_fwd_sm90(128, 128, causal=True, local=False)` 落在 `head_dim <= 128` 分支，返回 `FwdConfig(128, 128, RS=True, OL=True)`，即 `tile_m=128, tile_n=128`，与 causal 无关（这一档不区分掩码）。

**练习 2**：为什么 `head_dim=96` 那一档强制 `mma_pv_is_rs=False`（noRS），而 `head_dim=64` 和 `128` 却用 `True`（RS）？

**答案**：源码注释（[interface.py:140-141](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L140-L141)）明确说：在 Python kernel 上，`head_dim=96` 配 192× tile 时，把 PV 放寄存器（RS）会让性能从 ~600 TFLOPS 跌到 ~300 TFLOPS（寄存器压力过大导致溢出），所以这一档必须 noRS。其他头维没有这个问题，RS 反而更快。这是**实测出来的经验法则**，不是理论推导。

**练习 3**：SM100（Blackwell）的 tile 在 `_flash_attn_fwd` 里似乎没怎么选——为什么？

**答案**：SM100 用的是 persistent kernel + UMMA，tile 决策更多由 kernel 类 `FlashAttentionForwardSm100` 自己内部做（它还要兼顾 SplitKV、paged KV、2CTA 等特性）。`_flash_attn_fwd` 对 SM100 只传 `m_block_size=tile_m` / `n_block_size=tile_n`（取默认 128×128）和一堆特性开关，真正的 tile 调优发生在 kernel 内部。这部分会在 [u8-l1](u8-l1-blackwell-forward.md) 详讲。

## 5. 综合实践

把三个模块串起来：**用 `FLASH_ATTENTION_ARCH` 强制切换架构，跑同一个前向，验证「架构只换实现、不换数学」。**

> 下列脚本需要一张真实的 NVIDIA GPU（推荐 Hopper SM90，因为它对 SM80 的 PTX 向后兼容，便于在同一张卡上跑两条路径）。若你只有 Ampere 卡，把 `sm_90` 那次去掉即可；若只有 Blackwell，可改成 `sm_100` vs `sm_90` 对比。**完整双路径对比待本地验证。**

```python
# 示例代码：架构分发不影响数学结果
import subprocess, sys, torch

def run(arch_env):
    code = f'''
import os
os.environ["FLASH_ATTENTION_ARCH"] = "{arch_env}"   # 必须在 import 前设
import torch
from flash_attn.cute import flash_attn_func
torch.manual_seed(0)
q = torch.randn(2, 512, 8, 64, dtype=torch.float16, device="cuda")
k = torch.randn(2, 512, 8, 64, dtype=torch.float16, device="cuda")
v = torch.randn(2, 512, 8, 64, dtype=torch.float16, device="cuda")
out, lse = flash_attn_func(q, k, v, causal=True)
print(out.float().sum().item(), lse.sum().item())
'''
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    print(f"[{arch_env}] stderr tail:", out.stderr.strip().splitlines()[-3:] if out.stderr else "(none)")
    return out.stdout.strip()

a = run("sm_80")
b = run("sm_90")
print("sm_80:", a)
print("sm_90:", b)
print("一致？" , a == b)
```

**预期结果与解释**：两次的 `out.sum()` 与 `lse.sum()` 数值应当**几乎一致**（差异仅来自 fp16 舍入与不同 tile 下的累加顺序，量级在 1e-3 以内）。这正是本讲的核心结论：

> 架构分发、tile 选择、`num_threads`、`mma_pv_is_rs` 这些都只是**实现细节**——它们改变「用什么 MMA 指令、怎么分块、怎么流水」，但 FlashAttention 的数学定义（在线 softmax + tiling，精确注意力，见 [u1-l1](u1-l1-what-is-flashattention.md)）与架构无关。无论走 Sm80 还是 Sm90 kernel，算的都是同一个 \(\mathrm{softmax}(QK^\top/\sqrt{d})V\)，所以结果在数值精度范围内必然一致。

**进阶观察**：把 `out.stderr` 里关于「compiling kernel」的日志打开（设 `FA_LOG=info` 或观察首次调用耗时），你会看到两次分别编译了 `FlashAttentionForwardSm80` 与 `FlashAttentionForwardSm90` 两套不同的 kernel——这就直观证明了 `arch` 进了 `compile_key`、触发了两次独立的 JIT 编译。

## 6. 本讲小结

- FA4 在 **Python 层**用 `_get_device_arch()` 探测架构，返回整数 `arch`（80/90/100/120），再用 `arch // 10` 分发到五个 kernel 类之一。
- `FLASH_ATTENTION_ARCH` 环境变量可强行覆盖真实硬件；但因 `_get_device_arch` 有 `@lru_cache`，覆盖必须在进程首次调用前生效（最好用 shell 设）。
- 「kernel 选择」（`FLASH_ATTENTION_ARCH`）与「编译目标」（`CUTE_DSL_ARCH`）是两个独立层面，无 GPU 离线编译时两者都要设。
- `_validate_head_dims` 是编译前的早失败护栏：SM90 允许 8~256；SM100/110 只允许标准 8~128 加上 DeepSeek/MLA/hd256 几个特例形状；SM80/SM120 不走此校验。所有头维都必须整除 `alignment = 16 // element_size`（fp16 即 8）。
- `FwdConfig` 打包 `tile_m/tile_n/mma_pv_is_rs/intra_wg_overlap`；`_tile_size_fwd_sm90` 按 head_dim 分档、按 causal/local 微调 tile_n（头维越大 tile_n 越小，以控住 SMEM）；`num_threads` 在 SM80/SM120 为 128、SM90/SM100 为 384。
- 所有这些配置（`arch`、`tile_m`、`tile_n`、`num_threads`、两个布尔标志）都进 `compile_key`，因此改架构或改 tile 会触发重编译——但**只改变实现，不改变数学结果**。

## 7. 下一步学习建议

本讲把「分发到哪个 kernel 类、用什么 tile」讲完了，但**还没进任何 kernel 的内部**。自然的下一步：

- 想看 Sm80 kernel 的主循环怎么用这些 tile：读 [u6-l1 Ampere 前向 Kernel 全景](u6-l1-ampere-forward-kernel.md)。
- 想理解 `compile_key` 与 JIT 缓存的完整机制：读 [u11-l1 JIT 编译与缓存机制](u11-l1-jit-and-cache.md)。
- 想知道 `FA_CLC` / `FA_DISABLE_2CTA` 这两个本讲略过的开关到底做什么：读 [u8-l2 Tile Scheduler 与 CLC](u8-l2-tile-scheduler-clc.md) 与 [u8-l4 hd256 2CTA](u8-l4-hd256-2cta-kernel.md)。

建议直接精读 [`flash_attn/cute/interface.py:446-565`](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L446-L565) 这一段，把 arch→校验→tile→分发的完整链条在源码里再走一遍。
