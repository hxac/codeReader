# env.py 构建配置与 FFPA_\* 构建期变量

> 本讲是「手写 CUDA 后端与构建系统」单元的第 4 讲，承接 [u7-l3](u7-l3-per-headdim-codegen-pybind.md) 的 per-headdim 代码生成与 pybind 分发，聚焦 host 侧的「总指挥」——`env.py` 里的 `ENV` 类：它决定 **编什么、怎么编、为哪些 SM 编**。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `FFPA_BUILD_ARCH` 如何接受数字 SM（`"80,89,90"`）与架构别名（`ampere/hopper/...`）、如何归一化、空值时如何回退到当前显卡。
- 复述 head_dim 集合的**三级优先级**：`FFPA_DEV_HEADDIMS` > `ENABLE_FFPA_ALL_HEADDIM` > 默认 `range(256,1025,64)`，并能算出任一组合下会生成多少个 `.cu` 翻译单元（TU）。
- 解释 `ENABLE_FFPA_ALL_STAGES`（全集 `1~4` vs 精简集 `1~2`）通过 `-D` 宏在每个 TU 内部**改变模板实例化次数**、从而改变单 TU 编译时间的机制，并与「headdim 改变 TU 数量」区分开。
- 把 `ENV` 的布尔开关翻译成具体的 nvcc `-D` 宏与编译参数，理解 `setup.py` 如何消费 `ENV` 的四个核心方法。

## 2. 前置知识

本讲假设你已经读过 [u7-l1](u7-l1-cuda-fwd-kernel-architecture.md) 与 [u7-l3](u7-l3-per-headdim-codegen-pybind.md)，熟悉以下概念：

- **翻译单元（Translation Unit, TU）**：一个 `.cu`/`.cc` 源文件经预处理后的编译单位。nvcc 一次编译一个 TU，多个 TU 由 `MAX_JOBS` 并行编译。
- **模板实例化**：`launch_ffpa_attn_fwd_template<T, D, QK, PV, S>` 是一个 C++ 模板；给定一组模板参数，编译器会「实例化」出一份具体函数。`S`（流水线级数）每多一个取值，就多一次重型实例化。
- **`-D` 宏**：nvcc 的 `-DFOO` 等价于源码顶部 `#define FOO`，在预处理期决定 `#ifdef` 分支去留。
- **SM / compute capability**：显卡架构编号，如 `80`=Ampere、`89`=Ada、`90`=Hopper。`-gencode arch=compute_90,code=sm_90` 让 nvcc 为该架构生成代码。
- **构建隔离（build isolation）**：`pip install` 默认在隔离环境里构建；FFPA 源码构建常用 `--no-build-isolation` 让构建进程能看到本机的 `torch`/`nvcc`。

一句话定位：`setup.py` 只回答「**要不要**编 CUDA 扩展」，`env.py` 的 `ENV` 回答「**编什么、怎么编**」。本讲专门拆解后者。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `env.py` | `ENV` 类：所有构建期/运行期开关与构建参数生成 | 四个核心方法 + 别名表 + 三级 headdim 优先级 |
| `setup.py` | setuptools 入口，驱动 `CUDAExtension` | 如何把 `ENV` 的四个方法组装成 `sources` / `extra_compile_args` |
| `docs/env.md` | 全部环境变量的权威文档 | 构建期变量与「构建期 vs 运行期」区分 |
| `csrc/cuffpa/generated/` | 代码生成产物（已提交进仓库） | 用真实生成文件验证 headdim/stages 的效果 |
| `tools/build_fast.sh` | ccache + `MAX_JOBS` 加速构建包装 | `FFPA_DEV_HEADDIMS` 子集构建的实测耗时 |

## 4. 核心概念与源码讲解

本讲的四个最小模块，对应 `ENV` 的四个核心能力：**选 SM**（4.1）、**选 head_dim 集合**（4.2）、**按 head_dim 生成 TU**（4.3）、**把开关翻译成 nvcc 参数**（4.4）。

---

### 4.1 `get_build_arch_list`：SM 别名解析与当前设备回退

#### 4.1.1 概念说明

`FFPA_BUILD_ARCH` 决定「这次编译要为哪些显卡架构生成代码」。它有三种填法：

1. **数字 SM**：`"80,89,90"`，直接是 compute capability。
2. **架构别名**：`"ampere,ada,hopper"`，更易读。
3. **留空**：回退到当前可见显卡的 capability。

难点在于「归一化」：用户可能写 `"sm_90"`、`"compute_90"`、`"9.0"`、`"hopper"`、`"90+ptx"` 等各种形式，`get_build_arch_list` 要把它们统一成裸数字字符串 `"90"`，再交给 `setup.py` 拼成 `-gencode arch=compute_90,code=sm_90`。

#### 4.1.2 核心流程

```text
FFPA_BUILD_ARCH 非空？
├─ 是：按 [;,\s]+ 切分 → 每个 token 归一化：
│        去掉 "+ptx" 后缀 → 去 "sm_"/"compute_" 前缀 → 去小数点
│        → 查 _ARCH_ALIASES 别名表 → 去重
│        若结果为空 → 抛 RuntimeError
└─ 否（留空）：
     torch.cuda.is_available() 且 device_count>0？
     ├─ 是：读 current_device() 的 (major, minor) → 返回 ["{major}{minor}"]
     └─ 否：抛 RuntimeError（提示设置 FFPA_BUILD_ARCH）
```

注意：**留空 + 无 GPU** 是会直接报错的——这正是 docs/RTD CI 上需要 `FFPA_SKIP_CUDA_EXT=1` 的原因。

#### 4.1.3 源码精读

别名表把人类友好的代号映射到数字 SM：

[env.py:8-18](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L8-L18) —— `_ARCH_ALIASES` 字典：`ampere→80`、`hopper→90`、`blackwell→100`、`blackwell_geforce→120` 等。注释说明它镜像自 cache-dit 的 `CUDA_ARCH_ALIASES`，让用户可以用名字而非数字。

`get_build_arch_list` 的解析主体：

[env.py:161-198](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L161-L198) —— 逐 token 归一化并去重；空值分支用 `torch.cuda.get_device_capability` 回退，无卡则 `raise RuntimeError`。关键四步归一化：`removesuffix("+ptx")` → `removeprefix("sm_")`/`"compute_"` → `replace(".","")` → `_ARCH_ALIASES.get(norm, norm)`（查不到就当它本身就是数字）。

`setup.py` 消费这个列表，拼出 `-gencode` 参数：

[setup.py:116-119](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/setup.py#L116-L119) —— 对每个 SM 输出 `-gencode arch=compute_{sm},code=sm_{sm}`，最终并入 `nvcc` 编译参数（见 [setup.py:133-137](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/setup.py#L133-L137)）。

文档对 `FFPA_BUILD_ARCH` 的描述：

[docs/env.md:9](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L9) —— 默认空（当前设备 SM），接受数字或别名，空时回退当前显卡。

#### 4.1.4 代码实践

**实践目标**：验证别名归一化与回退逻辑。

**操作步骤**：

1. 在仓库根目录运行 `python3 env.py`，它会调用 `ENV.list_ffpa_env()` 打印所有 ENV 的当前值（见 [env.py:881-883](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L881-L883)）。
2. 分别用三种 `FFPA_BUILD_ARCH` 重复运行，观察 `FFPA_BUILD_ARCH` 那一行的输出：

```bash
FFPA_BUILD_ARCH="hopper"        python3 env.py   # 期望显示 -> 90
FFPA_BUILD_ARCH="sm_89,ampere"  python3 env.py   # 期望显示 -> 89,80（去重、归一化）
FFPA_BUILD_ARCH=""              python3 env.py   # 无 GPU 时抛 RuntimeError
```

**需要观察的现象**：`list_ffpa_env` 把解析结果用 `","` 重新拼接打印（[env.py:350](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L350)），所以别名会被还原成数字。

**预期结果**：别名正确解析、`sm_`/`compute_`/`.`/`+ptx` 都被去掉、结果去重。若无 GPU 且留空，应看到 `RuntimeError: FFPA_BUILD_ARCH is unset and no visible CUDA device is available ...`。

> 若当前环境无 GPU 或无 torch，本实践的「回退/报错」分支可在「待本地验证」标记下由你在带卡机器上确认。

#### 4.1.5 小练习与答案

**练习 1**：`FFPA_BUILD_ARCH="compute_8.9+ptx"` 会被解析成什么？
**答案**：`"89"`。流程是 `removesuffix("+ptx")` → `"compute_8.9"` → 去 `compute_` → `"8.9"` → 去 `.` → `"89"`，别名表查不到 `"89"` 就原样保留（`_ARCH_ALIASES.get(norm, norm)`）。

**练习 2**：为什么 docs CI（ReadTheDocs）机器上必须设 `FFPA_SKIP_CUDA_EXT=1`，而不能只靠「留空 `FFPA_BUILD_ARCH`」？
**答案**：因为留空会触发「回退到当前显卡」，而 RTD 机器无 GPU，`get_build_arch_list` 直接抛 `RuntimeError`。`FFPA_SKIP_CUDA_EXT=1` 在 `setup.py` 里把 `BUILD_CUDA_EXT` 强制置 False（[setup.py:83-87](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/setup.py#L83-L87)），整段 CUDA 扩展逻辑被跳过，`get_build_arch_list` 根本不会被调用。

---

### 4.2 `get_enabled_headdims`：head_dim 集合的三级优先级

#### 4.2.1 概念说明

FFPA 为每一个支持的 head_dim 单独生成 TU（见 4.3）。**head_dim 取值集合由三级优先级决定**，从高到低：

1. `FFPA_DEV_HEADDIMS`（开发期子集，如 `"256,512"`）——若非空，**直接替换**全集，用于只调某个 head_dim 时快速迭代。
2. `ENABLE_FFPA_ALL_HEADDIM=1`——全集为 `range(32, 1025, 32)`，即 32 的倍数、含 32 与 1024，共 32 个值。
3. 默认（`ENABLE_FFPA_ALL_HEADDIM=0` 且 `FFPA_DEV_HEADDIMS` 空）——`range(256, 1025, 64)`，即 64 的倍数、从 256 到 1024（含），共 13 个值。

这条优先级链是本讲最需要记住的结论。

#### 4.2.2 核心流程

```text
get_enabled_headdims():
  raw = FFPA_DEV_HEADDIMS.strip()
  if raw 非空:
      切分 → int → 去重 → 升序排序 → 返回
      （空列表则抛 RuntimeError）
  elif ENABLE_FFPA_ALL_HEADDIM:
      return list(range(32, 1025, 32))    # 32 个
  else:
      return list(range(256, 1025, 64))   # 13 个（默认）
```

注意三点：
- `FFPA_DEV_HEADDIMS` 一旦非空就**完全接管**，`ENABLE_FFPA_ALL_HEADDIM` 被忽略；
- 范围用的是 `range(..., 1025, ...)`，**包含 1024**（注释里写 `1024` 是不严谨的表述，以代码为准）；
- 解析失败（空列表）会 fail-fast 抛错，而不是静默回退。

#### 4.2.3 源码精读

两个开关字段的定义：

[env.py:28-37](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L28-L37) —— `ENABLE_FFPA_ALL_STAGES` 与 `ENABLE_FFPA_ALL_HEADDIM` 都是 `bool(int(envvar))` 解析。注意 `ALL_HEADDIM` 默认 `0`、`ALL_STAGES` 默认 `1`。

[env.py:155](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L155) —— `FFPA_DEV_HEADDIMS` 原始字符串字段（未解析，留给 `get_enabled_headdims` 处理）。

优先级主体：

[env.py:389-416](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L389-L416) —— `get_enabled_headdims` 的三级分支与 docstring。默认分支返回 `range(256, 1025, 64)`。

用仓库里**已生成的真实文件**佐证默认集合：`csrc/cuffpa/generated/` 下确实存在 `ffpa_attn_fwd_fp16_hdim256.cu` 到 `..._hdim1024.cu` 共 13 个 fp16 文件（步长 64），与 `range(256,1025,64)` 完全一致。

dispatch TU 的 `switch(d)` 也列出同样 13 个 case：

[csrc/cuffpa/generated/ffpa_attn_fwd_dispatch.cu:24-39](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/generated/ffpa_attn_fwd_dispatch.cu#L24-L39) —— `case 256 ... case 1024` 共 13 支，对未生成的 head_dim 抛 `"headdim not support!"`。

> **一个易踩的坑**：调试打印 `list_ffpa_env` 里默认集合的**显示字符串**写的是 `"range(320, 1024, 64)"`（[env.py:355-357](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L355-L357)），与 `get_enabled_headdims` 真正返回的 `range(256, 1025, 64)` **不一致**（起点 320 vs 256、终点不含 1024 vs 含）。这只是一个**纯展示用的陈旧标签**，不影响真实构建——真实集合以 `get_enabled_headdims` 与生成出的文件为准。调试时不要被这行 print 误导。

#### 4.2.4 代码实践

**实践目标**：用三种配置分别算出会生成多少个 `.cu` TU，并用 `python3 env.py` 验证。

**操作步骤**：

1. 推导（无需 GPU）：每个 head_dim 生成 **2 个** `.cu`（fp16 + bf16）+ 全集共享 **1 个** dispatch `.cu`。所以 `.cu` TU 数 = `len(headdims) × 2 + 1`。
2. 分别对三种配置算 TU 数：
   - 默认（13 个 head_dim）：`13×2+1 = 27` 个 `.cu` TU。
   - `ENABLE_FFPA_ALL_HEADDIM=1`（32 个）：`32×2+1 = 65` 个 `.cu` TU。
   - `FFPA_DEV_HEADDIMS="512"`（1 个）：`1×2+1 = 3` 个 `.cu` TU。
3. 运行 `python3 env.py` 对照 `FFPA_DEV_HEADDIMS` 那一行：

```bash
FFPA_DEV_HEADDIMS="512"           python3 env.py   # 显示 "512"
ENABLE_FFPA_ALL_HEADDIM=1         python3 env.py   # 显示 range(32,1024,32) 标签
python3                           env.py           # 默认显示 range(320,1024,64) 标签（见上文坑）
```

**需要观察的现象**：`FFPA_DEV_HEADDIMS` 一旦设置，`list_ffpa_env` 直接显示该字符串而非 range 标签（[env.py:353-358](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L353-L358)）。

**预期结果**：默认构建产出 27 个 `.cu` TU（与 `generated/` 目录现状一致：26 个 per-headdim `.cu` + 1 个 dispatch `.cu` + 1 个 `decls.h`）。

#### 4.2.5 小练习与答案

**练习 1**：`FFPA_DEV_HEADDIMS="512,256,512"` 解析结果是什么？
**答案**：`[256, 512]`。流程：切分得 `[512,256,512]` → 去重保序得 `[512,256]` → `sorted` 得 `[256,512]`（[env.py:400-413](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L400-L413)）。

**练习 2**：若同时设 `FFPA_DEV_HEADDIMS="512"` 和 `ENABLE_FFPA_ALL_HEADDIM=1`，会生成多少个 head_dim？
**答案**：只生成 `512` 一个。因为 `FFPA_DEV_HEADDIMS` 优先级最高，非空时直接 return，根本走不到 `enable_all_headdim()` 分支。

**练习 3**：默认集合是否包含 head_dim=128？
**答案**：不包含。默认是 `range(256,1025,64)`，最小是 256。要覆盖 128 必须显式 `FFPA_DEV_HEADDIMS="128,..."` 或 `ENABLE_FFPA_ALL_HEADDIM=1`（后者从 32 开始）。

---

### 4.3 `generate_split_headdim_sources` / `get_build_sources`：per-headdim 代码生成

#### 4.3.1 概念说明

承接 [u7-l3](u7-l3-per-headdim-codegen-pybind.md)：FFPA 把原本一个巨大的 `DISPATCH_HEADDIM` switch 拆成「每个 head_dim 一个 `.cu` TU」，让 `MAX_JOBS` 能在**文件间**并行编译，而不是只能在**单个文件内**串行。这套拆分由 `generate_split_headdim_sources` 在构建期现场生成源码，再由 `get_build_sources` 把它们连同 pybind 入口一起交给 setuptools。

关键设计：
- 生成文件**已提交进仓库**（不是 `.gitignore`），但每次构建仍会刷新——通过 `_write_if_changed` 仅在内容变化时落盘，保证稳态下不触发重编译。
- 只有 `enable_fwd_cuda_impl()` 为真（即 `ENABLE_FFPA_CUDA_IMPL=1`）才生成前向 TU；否则清理旧产物、返回空列表（Triton-only 模式下根本不编 `_C`）。

#### 4.3.2 核心流程

```text
get_build_sources(build_pkg):
  generated = generate_split_headdim_sources(build_pkg)
      ├─ gen_dir = csrc/cuffpa/generated/
      ├─ headdims = get_enabled_headdims()      # 4.2 的三级优先级
      ├─ if enable_fwd_cuda_impl():
      │     写 ffpa_attn_fwd_decls.h            # 每个 head_dim 3 条声明
      │     for d in headdims:
      │         写 ffpa_attn_fwd_fp16_hdim{d}.cu  # 2 个入口：fp16f16, fp16f32
      │         写 ffpa_attn_fwd_bf16_hdim{d}.cu  # 1 个入口：bf16f32
      │     写 ffpa_attn_fwd_dispatch.cu        # switch(d) 分发
      ├─ 清理 stale 文件（旧布局 / 关闭时的残留）
      └─ return [decls.h, *.cu ...]
  generated_sources = 只要 .cu 的
  return [csrc/cuffpa/ffpa_attn_api.cc] + generated_sources
```

回忆 [u7-l3](u7-l3-per-headdim-codegen-pybind.md) 的结论：**每个 head_dim 2 个文件、3 条声明**（fp16 文件含 `fp16f16`+`fp16f32` 两个入口，bf16 文件只有 `bf16f32` 一个入口，因为 bf16 没有 f16-acc 的 MMA PTX，必须 fp32 累加）。

#### 4.3.3 源码精读

代码生成主函数：

[env.py:440-511](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L440-L511) —— `generate_split_headdim_sources`。注意 [env.py:461](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L461) 取 headdim 集合，[env.py:465](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L465) 用 `enable_fwd_cuda_impl()` 守卫，[env.py:482](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L482) 算出 `fwd_generated_count = len(headdims) * 2 + 1`（与 4.2 的 TU 公式一致）。

每个 head_dim 的声明头渲染（每 d 三条声明）：

[env.py:515-537](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L515-L537) —— `_render_decls_header`，为每个 d 生成 `ffpa_attn_fwd_{fp16f16,fp16f32,bf16f32}_d{d}` 三条函数声明。

bf16 TU 只有单一入口（强制 fp32 累加）：

[env.py:628-652](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L628-L652) —— `_render_per_headdim_bf16_tu`，注释明确「bf16 has no f16-acc mma PTX; acc is forced to f32」，只渲染 `bf16f32_d{d}` 一个入口。

`get_build_sources` 把生成 TU 与 pybind 入口拼起来：

[env.py:734-758](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L734-L758) —— 返回 `[ffpa_attn_api.cc] + 生成 .cu`。注释解释了「拆 TU 让 `MAX_JOBS` 并行编译多个小文件、缩短重型 `launch_ffpa_attn_fwd_template` 实例化的总编译时间」这一核心动机。

`_write_if_changed` 是稳态增量构建的关键：

[env.py:422-438](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L422-L438) —— 内容不变就不写，避免无谓改 mtime 触发重编译。

`setup.py` 消费 sources：

[setup.py:121-143](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/setup.py#L121-L143) —— `CUDAExtension` 的 `sources` 来自 `ENV.get_build_sources(build_pkg=True)`，并把绝对路径转成仓库相对路径（editable 安装要求）。

#### 4.3.4 代码实践

**实践目标**：亲手触发一次「仅 head_dim=512」的代码生成，观察产物。

**操作步骤**：

```bash
# 1. 备份并清空生成目录（仅本实践用，勿提交）
cp -r csrc/cuffpa/generated /tmp/generated.bak

# 2. 只为 head_dim=512 重新生成（必须同时开启 CUDA 扩展）
ENABLE_FFPA_CUDA_IMPL=1 FFPA_DEV_HEADDIMS="512" python3 -c "
from env import ENV
srcs = ENV.generate_split_headdim_sources(build_pkg=False)
for s in srcs: print(s)
"

# 3. 观察产物
ls csrc/cuffpa/generated/

# 4. 还原
rm -rf csrc/cuffpa/generated && mv /tmp/generated.bak csrc/cuffpa/generated
```

**需要观察的现象**：步骤 2 应只打印 4 个文件——`ffpa_attn_fwd_decls.h`、`ffpa_attn_fwd_fp16_hdim512.cu`、`ffpa_attn_fwd_bf16_hdim512.cu`、`ffpa_attn_fwd_dispatch.cu`。`dispatch.cu` 的 `switch(d)` 只剩 `case 512` 一支。

**预期结果**：生成 1 个 decls.h + 2 个 per-headdim `.cu` + 1 个 dispatch `.cu` = 4 个文件，其中 `.cu` TU = 3 个，与 4.2.4 的公式 `1×2+1=3` 一致。

> 若不设 `ENABLE_FFPA_CUDA_IMPL=1`，`generate_split_headdim_sources` 走 [env.py:465](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L465) 的守卫，**不生成任何前向 TU**，反而会清理掉旧的 fwd 文件——这会破坏仓库。所以本实践务必带 `ENABLE_FFPA_CUDA_IMPL=1`，并记得步骤 4 还原。

#### 4.3.5 小练习与答案

**练习 1**：为什么生成文件已经提交进仓库，每次构建还要重新跑生成器？
**答案**：为了让 headdim/stages 配置变化后能自动更新产物；同时用 `_write_if_changed` 保证「内容没变就不落盘」，稳态下不触发重编译（[env.py:422-438](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L422-L438)）。

**练习 2**：fp16 TU 有两个入口、bf16 TU 只有一个，为什么？
**答案**：fp16 激活既可用 f16-acc 也可用 f32-acc（`fp16f16`/`fp16f32` 两个入口）；bf16 没有 bf16-acc 的 MMA PTX，累加器必须 fp32，所以只有 `bf16f32` 一个入口（[env.py:629-633](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L629-L633)）。

---

### 4.4 `get_build_cuda_cflags`：把 ENV 翻译成 nvcc 参数（含 stages 全集/精简集）

#### 4.4.1 概念说明

`get_build_cuda_cflags` 是「ENV → nvcc 命令行」的翻译器，把一堆布尔开关与固定选项拼成 nvcc 参数列表。它内部调用 `env_cuda_cflags()`，后者把每个 `ENABLE_FFPA_*` 开关翻成一个 `-DENABLE_FFPA_*` 宏。

本模块的重点是理解 **`ENABLE_FFPA_ALL_STAGES` 与 `ENABLE_FFPA_ALL_HEADDIM` 是构建期变量**（改了要重编），以及 `ALL_STAGES` 如何通过 `-D` 宏在每个 TU 内部改变模板实例化次数——这与「headdim 改变 TU 数量」是**两条正交的省时路径**。

#### 4.4.2 核心流程

```text
get_build_cuda_cflags():
  固定项：-O3 -std=c++17 -Xcompiler -fPIC
          -U__CUDA_NO_HALF_* / -U__CUDA_NO_BFLOAT16_CONVERSIONS__
          --expt-relaxed-constexpr --expt-extended-lambda --use_fast_math
  + env_cuda_cflags()           # 把每个 ENABLE_FFPA_* 翻成 -D 宏
  + -I csrc/cuffpa
  + -diag-suppress 177 / 1886
  if FFPA_PTXAS_VERBOSE: --ptxas-options=-v -Xptxas -v
  else:               --ptxas-options=-O3
  if FFPA_NVCC_THREADS>1: --threads={N}
  过滤空串 → return

env_cuda_cflags():  # 关键映射
  if enable_all_mutistages(): -DENABLE_FFPA_ALL_STAGES
  if enable_all_headdim():    -DENABLE_FFPA_ALL_HEADDIM
  ...（swizzle / persist / prefetch / force-acc 等运行期开关同名宏）
  + 若干 assert 校验 persist/swizzle 互斥关系
```

`-DENABLE_FFPA_ALL_STAGES` 的作用机制：生成器在每个 TU 里都写了 `#ifdef ENABLE_FFPA_ALL_STAGES ... #else ... #endif`（见 [env.py:562-580](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L562-L580)）。宏定义了就编译 4 个 stage 分支（实例化 `launch_ffpa_attn_fwd_template<S>` for S∈{1,2,3,4}），没定义就只编译 2 个（S∈{1,2}）。**所以 `ALL_STAGES=0` 把每个入口的模板实例化数砍半**，单 TU 编译时间约减半——但运行期可选的流水线级数也少了。

#### 4.4.3 源码精读

`get_build_cuda_cflags` 主体：

[env.py:760-791](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L760-L791) —— 固定项 + `env_cuda_cflags()` + ptxas/nvcc-threads 分支 + 过滤空串。注意 [env.py:780-785](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L780-L785) 的 `FFPA_PTXAS_VERBOSE` 开关与 [env.py:787-788](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L787-L788) 的 `--threads` 仅在 `>1` 时附加。

`env_cuda_cflags` 的宏映射与互斥校验：

[env.py:274-327](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L274-L327) —— 逐开关追加 `-D` 宏，[env.py:310-326](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L310-L326) 用 assert 强制 persist/swizzle 的互斥约束（如 `PERSIST_KV_G2S` 必须连同 `PERSIST_Q_G2S`、`PERSIST_Q_G2S` 与 `PERSIST_Q_S2R` 不能并存）。注意 [env.py:277-278](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L277-L278) 把 `ALL_STAGES` 翻成宏，这正是 stages 影响编译的入口。

stages 影响模板实例化的生成器代码：

[env.py:539-580](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L539-L580) —— `_render_stage_body`：`#ifdef ENABLE_FFPA_ALL_STAGES` 分支含 stage 2/3/4/1 四支，`#else` 分支只含 2/1 两支。每个分支都是一次 `launch_ffpa_attn_fwd_template<{S}>` 实例化。

`setup.py` 把 cflags 与 `-gencode` 合并：

[setup.py:131-138](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/setup.py#L131-L138) —— `nvcc` 参数 = `get_build_cuda_cflags(build_pkg=True) + cc_flag`（`cc_flag` 是 4.1 的 `-gencode` 列表）。

文档对「构建期 vs 运行期」的权威区分：

[docs/env.md:65-66](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L65-L66) —— 明确 `ALL_STAGES` 与 `ALL_HEADDIM` 是构建期变量，改了必须重建；其余 `ENABLE_FFPA_*` 多为运行期，无需重编。

[docs/env.md:15](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L15) —— `ENABLE_FFPA_ALL_STAGES=1` 生成 stages 1~4；`=0` 只生成 1~2，「减少 stage 选择以缩短构建时间」。

#### 4.4.4 代码实践

**实践目标**：对比 `ALL_STAGES=1` 与 `=0` 时 `env_cuda_cflags()` 的输出差异。

**操作步骤**：

```bash
# ALL_STAGES=1（默认）：应含 -DENABLE_FFPA_ALL_STAGES
python3 -c "from env import ENV; print(ENV.env_cuda_cflags())"

# ALL_STAGES=0：不应含该宏
ENABLE_FFPA_ALL_STAGES=0 python3 -c "from env import ENV; print(ENV.env_cuda_cflags())"
```

**需要观察的现象**：第一条输出列表里能找到 `"-DENABLE_FFPA_ALL_STAGES"`；第二条里找不到。这意味着第二条构建出的每个 TU 只会编译 `#else` 分支（stage 1/2）。

**预期结果**：`ALL_STAGES=0` 时每个入口符号的 `launch_ffpa_attn_fwd_template<S>` 实例化从 4 次降到 2 次，单 TU 编译时间显著下降，代价是运行期只能在 stage 1/2 间选择（更少的流水线调度候选）。

> 是否真的更快属「待本地验证」：在带 nvcc 的机器上分别计时 `ENABLE_FFPA_ALL_STAGES=0/1 bash tools/build_fast.sh` 即可量化。

#### 4.4.5 小练习与答案

**练习 1**：「减少 head_dim 数量」和「关 `ALL_STAGES`」分别从哪个维度缩短构建时间？
**答案**：前者减少 **TU 的数量**（文件间并行度更高、总实例化更少，由 `get_enabled_headdims` 控制）；后者减少 **每个 TU 内的模板实例化次数**（stage 分支数减半，由 `-DENABLE_FFPA_ALL_STAGES` 宏控制）。两者正交，可叠加。

**练习 2**：为什么 `FFPA_NVCC_THREADS` 默认只有 4，而不是越大越好？
**答案**：因为 per-headdim 拆 TU 后，外层 `MAX_JOBS` 已经在驱动**很多个 nvcc 进程**并行；若再给每个 nvcc 进程开大 `--threads`，会造成 CPU 过订阅（oversubscription）。`--threads` 只在 `MAX_JOBS` 较小时才有收益（[env.py:140-145](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L140-L145)）。

**练习 3**：改了 `ENABLE_FFPA_SMEM_SWIZZLE_Q` 需要重编吗？
**答案**：需要。虽然它被归类在「运行期 kernel 选择」，但它会被 `env_cuda_cflags()` 翻成 `-DENABLE_FFPA_SMEM_SWIZZLE_Q` 宏（[env.py:289-290](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L289-L290)），影响 SMEM 布局的编译期 `constexpr` 分支，所以改了要重建。只有 docs/env.md「Key notes」点名的 `ALL_STAGES`/`ALL_HEADDIM` 之外的纯运行时分支才可能免重编——实践中改任何 `-D` 宏都建议重编。

---

## 5. 综合实践：配置一次「仅 head_dim=512、stages 1-2」的快速构建

**任务**：用 `FFPA_DEV_HEADDIMS="512" ENABLE_FFPA_ALL_STAGES=0` 配置一次最小化 CUDA 构建，并定量解释它相比全量构建省下了哪些翻译单元与模板实例化。

**前置提醒**：`FFPA_DEV_HEADDIMS` 与 `ENABLE_FFPA_ALL_STAGES` 都只在「真正编译 CUDA 扩展」时才有意义，所以必须同时设 `ENABLE_FFPA_CUDA_IMPL=1`（否则走 Triton-only，零 TU，谈不上「省」）。

**操作步骤**：

```bash
# 1. 最小化构建（clean + 仅 512 + stages 1~2）
ENABLE_FFPA_CUDA_IMPL=1 \
FFPA_DEV_HEADDIMS="512" \
ENABLE_FFPA_ALL_STAGES=0 \
FFPA_CLEAN=1 \
bash tools/build_fast.sh
```

**定量解释（省下了什么）**：

以「默认全量构建」（`ENABLE_FFPA_CUDA_IMPL=1`，`FFPA_DEV_HEADDIMS` 空，`ALL_HEADDIM=0`，`ALL_STAGES=1`）为基线：

| 维度 | 全量构建（基线） | 本实践（512 + stages1~2） | 省下 |
|------|------------------|--------------------------|------|
| head_dim 集合 | `range(256,1025,64)` = 13 个 | `[512]` = 1 个 | 12 个 head_dim |
| `.cu` TU 数 | `13×2+1 = 27` | `1×2+1 = 3` | **24 个 `.cu` TU** |
| 每入口 stage 实例化 | 4 次（S=1,2,3,4） | 2 次（S=1,2） | 每入口减半 |
| fp16 TU 入口数 × stage | 2 入口 × 4 = 8 | 2 入口 × 2 = 4 | 4 |
| bf16 TU 入口数 × stage | 1 入口 × 4 = 4 | 1 入口 × 2 = 2 | 2 |
| `launch_ffpa_attn_fwd_template` 实例化总计 | `13×(8+4) = 156` | `1×(4+2) = 6` | **150 次重型实例化** |

**结论**：本实践把 `.cu` 翻译单元从 27 个砍到 3 个（省 24 个 TU），把重型模板实例化从约 156 次砍到 6 次（省 150 次）。两条省时路径叠加：head_dim 子集减少 **TU 数量**（更多并行 + 更少总量），`ALL_STAGES=0` 减少 **单 TU 内实例化次数**。`tools/build_fast.sh` 的实测注释也印证了子集构建的加速（冷构建从分钟级降到约 48s，见 [tools/build_fast.sh:113-117](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tools/build_fast.sh#L113-L117)）。

**验证产物**：构建后 `ls csrc/cuffpa/generated/` 应只剩 `ffpa_attn_fwd_decls.h`、`ffpa_attn_fwd_fp16_hdim512.cu`、`ffpa_attn_fwd_bf16_hdim512.cu`、`ffpa_attn_fwd_dispatch.cu` 四个文件，且 `dispatch.cu` 的 `switch(d)` 只剩 `case 512`。运行 `import ffpa_attn; ffpa_attn._C` 应可导入。

> 注意：本实践会把仓库的 `generated/` 目录与本地 `.so` 改动。它是「源码阅读+构建型」实践，建议在可重建的环境里做，做完后用 `git checkout csrc/cuffpa/generated/` 还原生成文件，避免污染工作区。**实际编译耗时待本地验证**（需要 nvcc + GPU 环境）。

## 6. 本讲小结

- `env.py` 的 `ENV` 类是构建「总指挥」：`setup.py` 只决定要不要编（`BUILD_CUDA_EXT`），`ENV` 决定编什么、怎么编。
- `FFPA_BUILD_ARCH` 支持数字 SM 与别名（`_ARCH_ALIASES`），经四步归一化（去 `+ptx`/前缀/小数点 + 别名查表）后交给 `setup.py` 拼 `-gencode`；留空回退当前显卡，无卡则报错。
- head_dim 集合遵循**三级优先级**：`FFPA_DEV_HEADDIMS` > `ENABLE_FFPA_ALL_HEADDIM`（`range(32,1025,32)`）> 默认（`range(256,1025,64)`），决定 `.cu` TU 数 = `len(headdims)×2+1`。
- `generate_split_headdim_sources` 在构建期生成 per-headdim TU（每 d 两文件三声明，bf16 必须 fp32 累加），`_write_if_changed` 保证稳态增量构建不重编。
- `get_build_cuda_cflags` + `env_cuda_cflags` 把 `ENABLE_FFPA_*` 翻译成 `-D` 宏；`ALL_STAGES=0` 经 `#ifdef` 把每个 TU 的 stage 模板实例化从 4 次砍到 2 次。
- **两条正交省时路径**：headdim 子集减 **TU 数量**，`ALL_STAGES=0` 减 **单 TU 实例化次数**；两者都属构建期变量，改了必须重编。

## 7. 下一步学习建议

- 接着读 [u7-l5（运行时 kernel 选择开关）](u7-l5-runtime-kernel-selection-knobs.md)：本讲的 `env_cuda_cflags()` 把 `ENABLE_FFPA_*` 翻成 `-D` 宏后，这些宏在 `launch_templates.cuh` 里如何驱动的 `constexpr` 分支与运行期 kernel 选择，是自然的下一站。
- 想理解「为什么要拆 TU」的工程动机，可重读 [tools/build_fast.sh](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tools/build_fast.sh) 的注释段（ccache shim + `MAX_JOBS` + per-headdim 并行）。
- 若关心这些生成的 TU 在运行期如何被 dispatch 到正确 kernel，回顾 [u7-l3](u7-l3-per-headdim-codegen-pybind.md) 的 `ffpa_attn_forward` → `ffpa_attn_fwd_{fp16f16,fp16f32,bf16f32}` → `..._d{D}` 三级分发链。
- 想做扩展（如新增 head_dim=448），参考本讲的 `get_enabled_headdims` 与 `generate_split_headdim_sources`，配合 [u9-l4（二次开发扩展指南）](u9-l4-extension-guide.md) 一起改。
