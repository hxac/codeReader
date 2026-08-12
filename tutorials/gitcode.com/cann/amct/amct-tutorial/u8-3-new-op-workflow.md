# 新增 NPU 算子的开发流程

## 1. 本讲目标

本讲是 `amct_ops` 算子开发的收口课。在上一讲（u8-l2）我们已经解剖了 `hifloat8_cast` 这一个算子「三层结构」（op_kernel / op_extension / python）的内部细节；本讲换个视角，回答一个**工程流程**问题：

> 「如果我要给 AMCT 新增一个 NPU 算子，到底要建哪些目录、写哪些文件、改不改构建脚本、注册到哪个命名空间？」

学完本讲你应该能够：

1. 说清一个新算子的**标准目录结构**，并区分 AMCT 现存的**两种算子范式**（直接扩展 vs CANN open project）。
2. 说清 `ops_build.sh` + `setup.py` 的**自动发现与打包机制**——以及它对哪种范式成立、对哪种范式需要特判。
3. 理解 `amct` 命名空间**唯一性约束**，以及 `torch.ops.amct.<op>` 背后的 `TORCH_LIBRARY` / `TORCH_LIBRARY_FRAGMENT` + `PrivateUse1` / `Meta` 四件套注册规范。
4. 以 `svd_quant` 为案例，看懂一个带 `op_host`（算子定义 + tiling）+ `op_kernel` 的 open project 算子是如何组织的。

本讲只讲「新增算子的流程与规范」，不深入 Ascend C kernel 的位运算细节（那是 u8-l2 的内容），也不讲量化算法本身。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **amct_ops 的定位**（u8-l1）：它是与 `amct_pytorch` 职责分离的 NPU 自定义算子层，用 Ascend C kernel 实现底层算子，产含 `.so` 的平台相关 wheel；`amct_pytorch` 运行时先探测 `torch_npu` 是否原生支持某算子，不支持才回退 import `amct_ops`。
- **三层结构与三层术语**（u8-l2）：op_kernel（device 端 Ascend C kernel）、op_extension（host 端 C++ binding + `TORCH_LIBRARY` 注册）、python（`.so` 加载与薄包装）；以及 GM/UB/AIV/tiling/MTE/dispatcher/PrivateUse1/Meta 等术语。
- **构建平台**（u8-l1）：`--soc`（`ascend910b` / `ascend910_93` / `ascend950`）映射到 `NPU_ARCH`（`dav-2201` / `dav-3510`）。

下面补充两个本讲要反复用到、但前面没展开的概念：

- **算子的两种调用路径**。AMCT 算子最终都要被 Python 调到，但中间有两条路：
  - **直接扩展路径**：kernel 源码直接编进扩展 `.so`，host 侧用一个普通 C++ 函数包住 device kernel 调用（如 `hifloat8_cast` 的 `AscendKernel::Hifloat8CastTorch`）。装一个 wheel 就能用。
  - **open project / aclnn 路径**：kernel 由 CANN 工具链单独编译、安装进 CANN 的 `opp` 目录，host 侧通过 `aclnnXxx` 这个**host API** 间接调用（如 `svd_quant` 的 `EXEC_NPU_CMD_V1(aclnnSvdQuant, ...)`）。除了装 wheel，还要先装一个 `.run` 包把算子注册进 CANN。
  - 你选哪条路径，决定了目录里要不要有 `op_host`、要不要写 tiling、要不要改 `ops_build.sh`——这是本讲的核心分叉点。
- **`TORCH_LIBRARY` 与 dispatcher**。PyTorch 用「命名空间 + 算子名」定位自定义算子（`torch.ops.<ns>.<op>`）。一个算子定义 schema 后，要为不同**后端**（dispatch key）挂不同实现：`PrivateUse1` 是 `torch_npu` 占用的 NPU 后端槽位，`Meta` 是只做形状推导、不真正算数的虚拟后端（`torch.compile` / 图模式需要它，否则报 "no Meta kernel"）。这套机制在 u8-l2 已建立，本讲从「新增算子要照抄哪几行」的角度再用一次。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [amct_ops/README.md](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/README.md) | 算子层总文档，含「新增算子」「命名空间约束」两节，是本讲的权威规范来源 |
| [amct_ops/ops_build.sh](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh) | 统一构建入口，含自动发现循环 + `svd_quant` 特判分支 |
| [amct_ops/setup.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/setup.py) | 统一 wheel 打包配置，从 `staging/` 收集包与 `.so` |
| [amct_ops/ops_init.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_init.py) | 打包时复制为 `amct_ops/__init__.py`，是包入口 |
| [amct_ops/hifloat8_cast/CMakeLists.txt](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/CMakeLists.txt) | 「直接扩展」范式的 CMake 模板 |
| [amct_ops/hifloat8_cast/op_extension/register.cpp](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_extension/register.cpp) | 直接扩展范式的注册样板（`FRAGMENT` + `PrivateUse1` + `Meta`） |
| [amct_ops/svd_quant/CMakeLists.txt](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/CMakeLists.txt) | 「open project」范式的 CMake（用 CANN 的 `op_host_aclnn`/`optiling` target） |
| [amct_ops/svd_quant/op_host/svd_quant_def.cpp](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/op_host/svd_quant_def.cpp) | open project 的算子 IR 定义（输入输出/dtype/目标 soc） |
| [amct_ops/svd_quant/op_host/svd_quant_tiling.h](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/op_host/svd_quant_tiling.h) | tiling 数据结构与 tiling 类声明 |
| [amct_ops/svd_quant/python/svd_quant/csrc/ops_def_registration.cpp](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/python/svd_quant/csrc/ops_def_registration.cpp) | open project 的 schema 注册（`TORCH_LIBRARY(amct, m)`） |
| [amct_ops/svd_quant/python/svd_quant/csrc/svd_quant.cpp](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/python/svd_quant/csrc/svd_quant.cpp) | `aclnnSvdQuant` 的 PrivateUse1 + Meta 实现 |
| [amct_ops/svd_quant/python/svd_quant/__init__.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/python/svd_quant/__init__.py) | `load_library` 加载 `.so` 的 Python 入口 |
| [amct_ops/svd_quant/python/setup.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/python/setup.py) | open project 范式下、算子自带的 `NpuExtension` 打包脚本 |

## 4. 核心概念与源码讲解

### 4.1 新算子的目录结构规范与两种范式

#### 4.1.1 概念说明

README 在「新增算子」一节给出了**标准目录骨架**，每个算子独占一个子目录。但只要把 `hifloat8_cast` 和 `svd_quant` 并排放，就会发现它们的目录长得**不一样**：

```
hifloat8_cast/          ← 直接扩展范式
├── op_kernel/          # Ascend C kernel
├── op_extension/       # PyTorch C++ binding + TORCH_LIBRARY 注册
├── python/<pkg>/       # Python 接口
├── CMakeLists.txt
└── README.md

svd_quant/              ← CANN open project 范式
├── op_host/            # 算子 IR 定义 + tiling（直接扩展范式没有这层）
├── op_kernel/          # Ascend C kernel
├── python/
│   ├── <pkg>/csrc/     # PyTorch host 实现 + TORCH_LIBRARY 注册
│   └── setup.py        # 算子自带的 NpuExtension 打包脚本
├── CMakeLists.txt
└── README.md
```

差别不在「风格」，而在**算子怎么被调用**：

- `hifloat8_cast` 走**直接扩展路径**：kernel 直接编进 `.so`，host 侧 C++ 函数包住 device 调用。目录里只需要 `op_kernel` + `op_extension`。
- `svd_quant` 走 **open project / aclnn 路径**：kernel 经 CANN 工具链编译、安装进 CANN `opp`，host 侧用 `aclnnSvdQuant` host API 调用。这套路径要求算子先在 CANN 侧「注册成一个合法算子」，于是多出 `op_host`（写算子 IR 定义和 tiling）这一层。

所以「新增算子」的第一步不是建目录，而是**先判断走哪条范式**：

| 判断依据 | 直接扩展范式 | open project 范式 |
|----------|--------------|-------------------|
| kernel 是否能独立编进 `.so` | 能（elementwise、LUT 类） | 不能（要用 CANN 的 matmul cube、tiling 框架） |
| 是否需要 host 侧 tiling | 否（或自己算 tile） | 是（用 `MultiCoreMatmulTiling`/`TCubeTiling`） |
| 调用入口 | C++ 函数 | `aclnnXxx` host API |
| 是否需要装 `.run` | 否 | 是（把算子注册进 CANN） |
| 平台限制 | 由 `NPU_ARCH` 决定，A2/A3/A5 都行 | 通常锁定单一 soc（`svd_quant` 仅 `ascend950`） |
| 现成样板 | `hifloat8_cast` | `svd_quant` |

#### 4.1.2 核心流程

新增一个算子的整体流程（先选范式，再填骨架）：

```text
1. 判断范式：kernel 简单且自包含 → 直接扩展；要用 CANN cube/tiling → open project
2. 建子目录 amct_ops/<新算子>/
   直接扩展：op_kernel/  op_extension/  python/<pkg>/  CMakeLists.txt  README.md
   open project：op_host/  op_kernel/  python/<pkg>/csrc/  python/setup.py  CMakeLists.txt  README.md
3. 写 device kernel（op_kernel，两种范式都要）
4. 写 host 侧：
   直接扩展 → op_extension/register.cpp（FRAGMENT + PrivateUse1 + Meta）
   open project → op_host/（def + tiling）+ python/<pkg>/csrc/（aclnn 调用 + 注册）
5. 写 CMakeLists.txt（照搬同范式样板）
6. 直接扩展范式：重新跑 ops_build.sh 即自动发现打包（无需改脚本）
   open project 范式：需在 ops_build.sh 的 build_op() 里加一条特判分支
7. 写 tests/amct_ops/test_<op>.py（不放算子源码目录）
```

#### 4.1.3 源码精读

README 给出的标准骨架（直接扩展范式优先）见 [amct_ops/README.md:L130-L143](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/README.md#L130-L143)，要点是每个算子独占子目录，结构与 `hifloat8_cast/` 对齐。

直接扩展范式的 CMake 入口很「常规」——一个 `add_library` 把 kernel 和 extension 一起编进一个 `.so`，见 [amct_ops/hifloat8_cast/CMakeLists.txt:L97-L101](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/CMakeLists.txt#L97-L101)：

```cmake
add_library(hifloat8_cast_ops SHARED
    op_kernel/hifloat8_cast_kernel.cpp
    op_extension/hifloat8_cast_torch.cpp
    op_extension/register.cpp
)
```

注意它声明了 `project(hifloat8_cast LANGUAGES ASC CXX)`（[L36-L38](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/CMakeLists.txt#L36-L38)），`ASC` 这个语言就是 CANN 的 Ascend C kernel 语言；平台选择靠 `[--npu-arch=${NPU_ARCH}]` 编译选项传给 ASC 文件（[L132-L135](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/CMakeLists.txt#L132-L135)）。

open project 范式的 CMake 则**完全不同**——它不自己 `add_library`，而是往 CANN 提供的若干既有 target 上挂源文件，见 [amct_ops/svd_quant/CMakeLists.txt:L10-L35](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/CMakeLists.txt#L10-L35)：

```cmake
add_ops_compile_options(OP_NAME SvdQuant OPTIONS --cce-auto-sync=off ...)

target_sources(op_host_aclnn PRIVATE op_host/svd_quant_def.cpp)   # host aclnn 源
target_sources(optiling        PRIVATE op_host/svd_quant_tiling.cpp)  # tiling 源
if (NOT BUILD_OPEN_PROJECT)
target_sources(opmaster_ct     PRIVATE op_host/svd_quant_tiling.cpp)  # 打 .run 包时用
endif ()
```

这里的 `op_host_aclnn` / `optiling` / `opmaster_ct` 都是 CANN open project 框架预定义的 target，`add_ops_compile_options` 则给 `SvdQuant` 这个算子挂 Ascend C 编译选项。这种 CMake **不能脱离 CANN 的 open project 框架单独跑**，所以 `ops_build.sh` 对它要特殊处理（见 4.2）。

#### 4.1.4 代码实践

**实践目标**：用肉眼分辨两个算子的范式，不靠记忆靠目录。

**操作步骤**：

1. 在仓库根目录执行 `find amct_ops -maxdepth 2 -type d` 看二级目录。
2. 对照本讲 4.1.1 的两张目录树，给 `hifloat8_cast` 和 `svd_quant` 各自标注「有没有 `op_host`」「有没有 `op_extension`」「有没有自带 `python/setup.py`」。
3. 打开两个 `CMakeLists.txt`，搜 `add_library` 与 `target_sources(op_host_aclnn`，确认它们用了不同的构建动词。

**需要观察的现象**：`hifloat8_cast` 有 `op_extension`、无 `op_host`、CMake 用 `add_library`；`svd_quant` 有 `op_host`、无 `op_extension`、CMake 用 `target_sources(op_host_aclnn ...)` 且带 `BUILD_OPEN_PROJECT` 判断。

**预期结果**：能仅凭「有没有 `op_host`」这一条，判定一个算子是不是 open project 范式。

#### 4.1.5 小练习与答案

**练习 1**：假如你要加一个「逐元素把 BF16 压成 INT8」的简单算子，应选哪种范式？为什么？

> **答**：直接扩展范式。它是 elementwise、自包含的，kernel 能直接编进 `.so`，不需要 CANN 的 cube/tiling 框架，也不需要装 `.run`。照搬 `hifloat8_cast` 即可。

**练习 2**：`svd_quant` 的 CMakeLists 里 `if (NOT BUILD_OPEN_PROJECT)` 这段把 `svd_quant_tiling.cpp` 同时挂到 `opmaster_ct`，这暗示了什么？

> **答**：`BUILD_OPEN_PROJECT` 区分两种产物：打开时只编 host aclnn 库；关闭（打 `.run` 包）时还要编 `opmaster_ct`（CANN 算子包的 master target），把 tiling 也打进可安装的算子包。同一份 tiling 源在两条产物线上都要用。

---

### 4.2 ops_build.sh 的自动发现与统一打包机制

#### 4.2.1 概念说明

`ops_build.sh` 是所有算子的**统一构建入口**，它的设计目标是：新增一个（直接扩展范式的）算子后，**无需修改任何构建脚本**，重跑 `bash ops_build.sh` 就能自动编译并打包进 wheel。这套机制分四步流水线（脚本里的 `[1/4]`~`[4/4]`）：

```text
[1/4] 加载 CANN 环境（source set_env.sh）
[2/4] 编译算子      ← 自动发现：遍历 */，凡是有 CMakeLists.txt 的子目录都编译
[3/4] 汇集到 staging/ ← 自动发现：凡是有 python/<pkg>/ 的子目录都收集 *.py 和 .so
[4/4] 构建 wheel     ← setup.py 从 staging/ 统一打包
```

「自动发现」是关键：脚本不维护算子清单，而是用两条 shell 循环扫描目录。但要注意——**自动发现只对直接扩展范式成立**；open project 范式（`svd_quant`）目前是被硬编码特判的。

#### 4.2.2 核心流程

自动发现的两个循环：

```text
编译循环（build_op）：
  for op in */:
      存在 op/CMakeLists.txt → build_op(op)
          ↓
      op == "svd_quant" ?  → 走 open project 特判（需 ascend950）
                          → 否则走通用 cmake -S/-B + cmake --build

收集循环（collect_op）：
  for op in */:
      存在 op/python/ → 进 op/python/<pkg>/
          cp *.py      → staging/amct_ops/<pkg>/
          cp build/*.so → staging/amct_ops/<pkg>/
```

最后 `pip wheel .` 用根 `setup.py` 把 `staging/` 打成 `dist/amct_ops-1.0.0-cp*-cp*-linux_<arch>.whl`。

#### 4.2.3 源码精读

**编译阶段的自动发现**——脚本头部就声明了「新增算子无需改脚本」的约定，见 [amct_ops/ops_build.sh:L37-L39](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L37-L39)：

```bash
# 新增算子：
#   1. 在 <op>/python/<pkg>/ 下放 __init__.py，<op>/CMakeLists.txt 构建 .so
#   2. 重新运行此脚本即可自动打包
```

真正的发现循环在 [amct_ops/ops_build.sh:L158-L162](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L158-L162)：

```bash
for op in */; do
    op="${op%/}"
    [ -f "$op/CMakeLists.txt" ] && build_op "$op"
done
```

`build_op` 内部对 `svd_quant` 做了特判，其余走通用路径，见 [amct_ops/ops_build.sh:L123-L152](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L123-L152)。通用路径就是普通的外部构建：

```bash
else
    rm -rf "$op_dir/build" && mkdir "$op_dir/build"
    cmake -S "$op_dir" -B "$op_dir/build" \
        -DCMAKE_BUILD_TYPE=Release \
        -DNPU_ARCH="${NPU_ARCH}" \
        -DASCEND_ARCH_DIR="${CANN_ARCH_DIR}" > /dev/null
    cmake --build "$op_dir/build" -j"$(nproc)"
fi
```

也就是：给算子的 `CMakeLists.txt` 传两个变量——`NPU_ARCH`（`dav-2201`/`dav-3510`）和 `ASCEND_ARCH_DIR`（`x86_64-linux`/`aarch64-linux`）。**新算子的 CMakeLists 必须接受这两个变量**，这也是 4.1.3 里 `hifloat8_cast` 的 CMake 顶部那两段 `set(... CACHE ...)` 的来历。

`svd_quant` 的特判分支（[L123-L144](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L123-L144)）多了三件事：①平台必须是 `ascend950`，否则进 `SKIP_OP_LIST` 跳过；②用 `-DBUILD_OPEN_PROJECT=ON` 跑 CANN 的 open project cmake，`--target package` 产出 `.run` 安装包；③另跑一次 `python3 setup.py bdist_wheel` 编出扩展 `.so`，并把它统一改名成 `libsvd_quant.so`。这正解释了为什么 `svd_quant` 还自带一个 [amct_ops/svd_quant/python/setup.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/python/setup.py)（用 `torch_npu` 的 `NpuExtension`，见 [L27-L33](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/python/setup.py#L27-L33)），而 `hifloat8_cast` 没有。

**收集阶段**——`collect_op` 扫的是 `python/` 下的包目录，见 [amct_ops/ops_build.sh:L180-L193](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L180-L193)：

```bash
for pkg_dir in "$python_src"/*/; do      # 发现 python/<pkg>/
    [[ "$pkg_name" == _* || "$pkg_name" == .* ]] && continue   # 跳过 _ 私有 / 隐藏目录
    find "$pkg_dir" -maxdepth 1 -name "*.py" -exec cp {} "$dst/" \;
    [ -d "$op_dir/build" ] && find "$op_dir/build" -maxdepth 1 -name "*.so" -exec cp {} "$dst/" \;
done
```

注意两个细节：① 包名以 `_` 或 `.` 开头的目录会被跳过，所以 `python/svd_quant/csrc/` 这种 `csrc` 虽是目录却**不会**被打成 Python 包（它只是 C++ 源目录）；真正进 wheel 的是 `python/svd_quant/`（包名 `svd_quant`）。② `.so` 是从 `<op>/build/` 顶层收集的，所以 `svd_quant` 特判分支末尾要把 `.so` 改名搬到 `build/libsvd_quant.so`（[L143](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L143)）正是为了对齐这条收集规则。

**打包阶段**——根 [amct_ops/setup.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/setup.py) 以 `staging/` 为包根，用 `find_packages(where='staging')` 发现包，并用 `BinaryDistribution` 强制声明这是含扩展的平台相关 wheel（[L35-L39](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/setup.py#L35-L39)）。`.so` 的收集在 [L44-L54](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/setup.py#L44-L54)：它扫 `staging/amct_ops/<sub>/` 下的 `.so` 填进 `package_data`。包入口 `amct_ops/__init__.py` 其实是 `ops_init.py` 在收集阶段被复制过去的（[amct_ops/ops_build.sh:L167](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/ops_build.sh#L167)）。

#### 4.2.4 代码实践

**实践目标**：读懂「自动发现」的边界——它对什么成立、对什么不成立。

**操作步骤**：

1. 在仓库根执行 `grep -n "svd_quant" amct_ops/ops_build.sh`，统计 `svd_quant` 这个字符串在脚本里出现的行。
2. 再执行 `grep -n "hifloat8_cast" amct_ops/ops_build.sh`，对比出现次数。

**需要观察的现象**：`svd_quant` 在脚本里被**硬编码**多次（特判分支、平台检查、改名）；`hifloat8_cast` 在脚本里**几乎不出现**（只在注释/示例里）。

**预期结果**：得出结论——`hifloat8_cast`（直接扩展范式）享受「加目录即自动打包」；`svd_quant`（open project 范式）**不享受**，新增同类算子必须在 `build_op()` 里照抄一段特判分支。这是 README「无需修改 ops_build.sh」承诺的真实适用范围。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `collect_op` 里要跳过 `_` 开头的包目录？

> **答**：因为像 `python/svd_quant/csrc/` 是 C++ 源码目录，不是要发布给用户的 Python 包；用 `_`/`.` 前缀约定把它排除在打包之外，避免把源码目录误当成包打进 wheel。

**练习 2**：如果不改 `ops_build.sh`，直接在 `amct_ops/myop/` 下放一个直接扩展范式的算子，`bash ops_build.sh` 会编译它吗？会打包它吗？

> **答**：会。编译循环 `[ -f "$op/CMakeLists.txt" ] && build_op "$op"` 会发现它并编译；收集循环会发现 `myop/python/<pkg>/` 并把 `.py` 和 `build/*.so` 拷进 `staging/`；根 `setup.py` 的 `find_packages` 会自动把它收进 wheel。全程不用改脚本。

---

### 4.3 amct 命名空间约束与 torch.ops 注册规范

#### 4.3.1 概念说明

README「命名空间约束」一节是本节的权威：**所有算子必须注册到 `amct` 命名空间**，与包名 `amct_ops` 呼应，目的是让调用方一眼区分 AMCT 自定义算子与 `torch_npu` 上游算子（后者在 `torch_npu` 命名空间或 `npu_*` 函数下）。

这条约束落到代码上是一套「四件套」：

| 角色 | 宏 / 调用 | 作用 |
|------|-----------|------|
| 声明 schema | `TORCH_LIBRARY_FRAGMENT(amct, m)` 里 `m.def("op(...) -> ...")` | 把算子签名登记到 `amct` 命名空间 |
| NPU 实现 | `TORCH_LIBRARY_IMPL(amct, PrivateUse1, m)` 里 `m.impl("op", &fn)` | 给 NPU 后端挂真实实现 |
| 形状推导 | `TORCH_LIBRARY_IMPL(amct, Meta, m)` 里 `m.impl("op", &meta_fn)` | 给图模式/torch.compile 挂 Meta 实现 |
| Python 调用 | `torch.ops.load_library(path)` 后 `torch.ops.amct.op(...)` | 加载 `.so`，按命名空间调用 |

此外还有一条**唯一性约束**：算子名在 `amct` 内必须唯一，新增前要先查 `torch.ops.amct` 是否已有同名算子（见 [amct_ops/README.md:L145-L151](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/README.md#L145-L151)）。

#### 4.3.2 核心流程

一次 `torch.ops.amct.svd_quant(x, w, s, d, u)` 调用的分发链：

```text
Python:  torch.ops.amct.svd_quant(...)
   ↓  （import amct_ops.svd_quant 时已 torch.ops.load_library("libsvd_quant.so")）
dispatcher 按 namespace=amct + op=svd_quant 查 schema
   ↓
按 dispatch key 选实现：
   输入在 NPU → PrivateUse1 → svd_quant_npu() → EXEC_NPU_CMD_V1(aclnnSvdQuant, ...)
   图模式/形状推导 → Meta     → svd_quant_meta() → 只返回 empty 输出形状
```

#### 4.3.3 源码精读

README 明文规定 C++ 侧用 `TORCH_LIBRARY_FRAGMENT` + `TORCH_LIBRARY_IMPL(... PrivateUse1 ...)`，见 [amct_ops/README.md:L149](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/README.md#L149)。

**直接扩展范式的注册样板**——`hifloat8_cast` 把 schema、NPU 实现、Meta 实现三件全写在同一个 `register.cpp`，用 `FRAGMENT` 安全地追加进 `amct` 命名空间，见 [amct_ops/hifloat8_cast/op_extension/register.cpp:L24-L64](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/hifloat8_cast/op_extension/register.cpp#L24-L64)：

```cpp
TORCH_LIBRARY_FRAGMENT(amct, m) {                 // 声明 schema
    m.def("encode_to_hifloat8(Tensor input) -> Tensor");
    m.def("decode_from_hifloat8(Tensor input, ScalarType? dtype=None) -> Tensor");
}
TORCH_LIBRARY_IMPL(amct, PrivateUse1, m) {        // NPU 实现
    m.impl("encode_to_hifloat8", TORCH_FN(EncodeImpl));
    m.impl("decode_from_hifloat8", TORCH_FN(DecodeImpl));
}
TORCH_LIBRARY_IMPL(amct, Meta, m) {               // 形状推导
    m.impl("encode_to_hifloat8", &EncodeMeta);
    m.impl("decode_from_hifloat8", &DecodeMeta);
}
```

**为什么用 `FRAGMENT` 而不是 `TORCH_LIBRARY`？** 因为 `amct_ops` 会被编成**多个独立 `.so`**（每个算子一个），它们各自都要往 `amct` 命名空间加东西。`TORCH_LIBRARY_FRAGMENT` 允许多个扩展向同一命名空间**追加**而互不冲突；`TORCH_LIBRARY` 则倾向于「我拥有这个命名空间」。这正是 README 把 `FRAGMENT` 列为规范的原因。

**open project 范式的注册分两半**。schema 声明在一个专门的文件里，见 [amct_ops/svd_quant/python/svd_quant/csrc/ops_def_registration.cpp:L14-L18](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/python/svd_quant/csrc/ops_def_registration.cpp#L14-L18)：

```cpp
TORCH_LIBRARY(amct, m) {
    m.def("svd_quant(Tensor activation, Tensor weights, Tensor scales, Tensor down, Tensor up) -> Tensor");
}
```

注意这里用的是 `TORCH_LIBRARY`（非 `FRAGMENT`），与 README 推荐的 `FRAGMENT` 不一致——这是 `svd_quant` 这个历史样板的写法。**新增算子仍应优先按 README 用 `TORCH_LIBRARY_FRAGMENT`**，这样你的 `.so` 和别的算子的 `.so` 同时加载时不会因命名空间归属冲突。schema 一旦声明，`svd_quant` 这个名字就在 `amct` 内被占用了，这正对应「算子名在 amct 内须唯一」的约束。

PrivateUse1 + Meta 实现在另一半，见 [amct_ops/svd_quant/python/svd_quant/csrc/svd_quant.cpp:L39-L45](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/python/svd_quant/csrc/svd_quant.cpp#L39-L45)：

```cpp
TORCH_LIBRARY_IMPL(amct, PrivateUse1, m) { m.impl("svd_quant", &::svd_quant_npu); }
TORCH_LIBRARY_IMPL(amct, Meta, m)        { m.impl("svd_quant", &::svd_quant_meta); }
```

PrivateUse1 实现的核心是构造输出张量后把活儿交给 `aclnnSvdQuant` host API（[L17-L29](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/python/svd_quant/csrc/svd_quant.cpp#L17-L29)）：

```cpp
at::Tensor svd_quant_npu(...) {
    ...
    at::Tensor output = at::empty(oShape, ...);
    EXEC_NPU_CMD_V1(aclnnSvdQuant, activation, weights, scales, down, up, output);
    return output;
}
```

这就是 open project 范式「走 aclnn」的特征：host 侧只做形状检查和输出分配，真正的计算在 CANN 侧注册好的 `aclnnSvdQuant` 里。`EXEC_NPU_CMD_V1` 是 `ops_common.h` 里的宏，它会按名查 `aclnnSvdQuantGetWorkspaceSize` 与 `aclnnSvdQuant` 两个符号地址并调用（详见 [amct_ops/svd_quant/python/svd_quant/csrc/ops_common.h:L387-L442](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/python/svd_quant/csrc/ops_common.h#L387-L442)）。所以 open project 算子必须**先装 `.run` 包**让 CANN 认识 `aclnnSvdQuant`，否则这里会报「not found」。

**Python 侧**只需一行 `load_library` 把 `.so` 加载进来，schema 与实现就自动注册到 dispatcher，见 [amct_ops/svd_quant/python/svd_quant/__init__.py:L17-L18](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/python/svd_quant/__init__.py#L17-L18)：

```python
_lib_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libsvd_quant.so")
torch.ops.load_library(_lib_path)
```

之后两种调用方式都可用：`torch.ops.amct.svd_quant(...)`，或经模块包装 `from amct_ops.svd_quant import svd_quant`（注意 `svd_quant` 这个包 `__init__.py` 把算子名导出在 `__all__` 里）。

#### 4.3.4 代码实践

**实践目标**：验证「同一 `amct` 命名空间、多个算子共存」的现象（需 NPU 环境；无环境则改为源码阅读）。

**操作步骤**（有 NPU 时）：

1. `pip install dist/amct_ops-*.whl`。
2. 进 Python：
   ```python
   import amct_ops.hifloat8_cast   # 加载 libhifloat8_cast_ops.so
   print(torch.ops.amct.encode_to_hifloat8)   # 应能访问
   print(torch.ops.amct.svd_quant)            # 若已装 svd_quant 的 .run，也应能访问
   ```

**操作步骤**（无 NPU，源码阅读型）：

1. 用 `grep -rn "TORCH_LIBRARY" amct_ops/*/op_extension amct_ops/*/python` 找出所有 schema 声明点。
2. 用 `grep -rn "PrivateUse1\|, Meta," amct_ops` 找出所有实现挂载点。

**需要观察的现象**：`hifloat8_cast` 与 `svd_quant` 的 schema 都登记在 `amct` 命名空间下，算子名不同、互不冲突；每个算子都成对出现 `PrivateUse1` 和 `Meta` 两个 impl。

**预期结果**：能列出一张表——`encode_to_hifloat8` / `decode_from_hifloat8` / `svd_quant` 三个算子名，都挂在 `torch.ops.amct` 下，证明「多算子共享 amct 命名空间」成立。

> 待本地验证：上述 Python 内省命令需在装好 wheel（open project 算子还需装 `.run`）的 NPU 环境运行。

#### 4.3.5 小练习与答案

**练习 1**：`svd_quant` 用 `TORCH_LIBRARY`，`hifloat8_cast` 用 `TORCH_LIBRARY_FRAGMENT`，二者都能把算子加到 `amct`。新增算子该学哪个？

> **答**：学 `hifloat8_cast` 的 `TORCH_LIBRARY_FRAGMENT`。因为 amct_ops 是多 `.so` 并存，`FRAGMENT` 天然支持多个扩展向同一命名空间追加；README 也把它列为规范。`svd_quant` 的 `TORCH_LIBRARY` 是历史样板，不必照搬。

**练习 2**：如果你忘了写 `Meta` impl，算子还能跑吗？什么场景会出问题？

> **答**：直接 `torch.ops.amct.<op>(x)` 调用能跑（走 PrivateUse1）。但在 `torch.compile` / 图捕获 / 形状推导场景下，dispatcher 找不到 `Meta` 实现会报类似 "no Meta kernel" 的错。所以四件套里 Meta 不是装饰，是图模式必需。

---

### 4.4 svd_quant 案例：open project 算子的 op_host + op_kernel

#### 4.4.1 概念说明

`svd_quant` 是 AMCT 里 open project 范式的代表，它的数学含义是「混合 MxFp4/BF16 的 SVD 量化矩阵乘」，把激活异常值吸收到低秩分支里：

\[
\text{Out} = X \cdot L_1 \cdot L_2 \;+\; Q(X) \cdot Q(R)
\]

- \(X\) 是 BF16 激活，\(L_1, L_2\) 是低秩分支（BF16），\(R\) 是权重的剩余部分（压成 MxFp4），\(Q(\cdot)\) 是量化。
- 高精度低秩分支处理异常值，低精度分支跑主体 matmul，二者相加。

这个算子内部要调 CANN 的 matmul cube、要做复杂的 tiling，所以它不能像 `hifloat8_cast` 那样自包含地编进 `.so`，而必须走 open project：先在 CANN 侧把算子定义、tiling、kernel 注册成一个合法算子（产出 `.run` 安装包），再用扩展 `.so` 通过 `aclnnSvdQuant` 调用它。这也使它**只支持 Ascend950**。

#### 4.4.2 核心流程

open project 算子从源码到调用的完整链条：

```text
源码侧：
  op_host/svd_quant_def.cpp     → 定义算子 IR（输入输出/dtype/目标 soc）+ OP_ADD 注册
  op_host/svd_quant_tiling.h/.cpp → 定义 tiling 数据结构 + tiling 计算类
  op_kernel/svd_quant.cpp       → device 端 Ascend C kernel（cube matmul 等）
  python/<pkg>/csrc/            → host 侧 aclnn 调用 + TORCH_LIBRARY 注册

构建侧：
  ops_build.sh --soc ascend950 svd_quant
    → cmake -DBUILD_OPEN_PROJECT=ON ... --target package  → output/CANN-custom_ops-*.run
    → 装 .run 到 $ASCEND_HOME_PATH/opp（CANN 认识 aclnnSvdQuant）
    → python setup.py bdist_wheel                          → libsvd_quant.so
    → 汇进 amct_ops wheel

运行侧：
  torch.ops.amct.svd_quant(x,w,s,d,u)
    → svd_quant_npu → EXEC_NPU_CMD_V1(aclnnSvdQuant, ...)
    → CANN 找到已注册的算子 → 跑 tiling + kernel
```

#### 4.4.3 源码精读

**算子 IR 定义**（`op_host/svd_quant_def.cpp`）——这是 CANN GE（Graph Engine）侧的算子声明，逐个声明输入/输出的名字、必填性、数据类型、格式，最后绑定目标 soc，见 [amct_ops/svd_quant/op_host/svd_quant_def.cpp:L20-L56](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/op_host/svd_quant_def.cpp#L20-L56)：

```cpp
class SvdQuant : public OpDef {
public:
    explicit SvdQuant(const char *name) : OpDef(name) {
        this->Input("A").ParamType(REQUIRED).DataType({ge::DT_BF16})...;
        this->Input("W").ParamType(REQUIRED).DataType({ge::DT_UINT8})...;  // fp4x2 打包成 uint8
        this->Input("SC").ParamType(REQUIRED).DataType({ge::DT_UINT8})...; // e8m0 scale
        this->Input("DP").ParamType(REQUIRED).DataType({ge::DT_BF16})...;
        this->Input("UP").ParamType(REQUIRED).DataType({ge::DT_BF16})...;
        this->Output("O").ParamType(REQUIRED).DataType({ge::DT_BF16})...;
        this->AICore().AddConfig("ascend950");   // 只支持 Ascend950
    }
};
OP_ADD(SvdQuant);
```

`OP_ADD(SvdQuant)` 把这个算子定义注册进 CANN。注意 `W`（权重）是 `DT_UINT8`——MxFp4 两个 4-bit 元素打包成一个 uint8，与 README 接口表里 `float4_e2m1fn_x2` 对应；`SC`（scale）是 `DT_UINT8`，对应 `float8_e8m0fnu`（e8m0 共享指数，回顾 u2-l2 的 Microscaling）。

**tiling 结构与类**（`op_host/svd_quant_tiling.h`）。tiling 是 host 侧根据输入形状和平台算好的、传给 device kernel 的「分块参数包」。先用宏定义这个参数包的数据结构，见 [amct_ops/svd_quant/op_host/svd_quant_tiling.h:L29-L41](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/op_host/svd_quant_tiling.h#L29-L41)：

```cpp
BEGIN_TILING_DATA_DEF(SvdQuantTilingData)
TILING_DATA_FIELD_DEF(int32_t, batchSize);
TILING_DATA_FIELD_DEF(int32_t, M);
TILING_DATA_FIELD_DEF(int32_t, K);
TILING_DATA_FIELD_DEF(int32_t, N);
TILING_DATA_FIELD_DEF(int32_t, R);
TILING_DATA_FIELD_DEF_STRUCT(TCubeTiling, fp4MMTilingData);          // 三个 matmul 的 cube tiling
TILING_DATA_FIELD_DEF_STRUCT(TCubeTiling, downProjectionTilingData);
TILING_DATA_FIELD_DEF_STRUCT(TCubeTiling, upProjectionTilingData);
END_TILING_DATA_DEF;

REGISTER_TILING_DATA_CLASS(SvdQuant, SvdQuantTilingData)   // 把 tiling 结构绑定到 SvdQuant 算子
```

`M/K/N/R` 是矩阵乘的维度（见 README 参数表），三个 `TCubeTiling` 子结构分别对应公式里 `X·L1`、`·L2`、`Q(X)·Q(R)` 三次 matmul 的分块。`SvdQuantTiling` 类（[L45-L68](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/op_host/svd_quant_tiling.h#L45-L68)）负责在 host 上算这些值，它组合了 `ValidateShapes()`（校验形状）、`ReadShapes()`（读输入维度）和一组 `CalcB16MatmulTiling` / `CalcMxMatmulTiling`（BF16 与 MxFp4 两种 matmul 的 tiling 计算），本质就是 u8-l2 讲过的「host 查平台核数/UB 算好后单向只读传给 device」的 tiling 契约。

**device kernel**（`op_kernel/svd_quant.cpp`）——用 Ascend C 的 `MatmulImpl` 模板为低秩分支和 fp4 分支各定义一份 cube matmul 配置，见 [amct_ops/svd_quant/op_kernel/svd_quant.cpp:L32-L45](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/op_kernel/svd_quant.cpp#L32-L45)。它用 `__aicore__ inline constexpr` 为 down/up 投影各定制 `MatmulConfig`，并按 `MmStage` 枚举把 tiling 字段映射到对应 matmul，体现「一个 kernel 串联三次 matmul + 一次量化」的结构。

**测试落位**。`svd_quant` 的精度/功能测试不在算子目录里，而在 [tests/amct_ops/test_svd_quant.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/tests/amct_ops/test_svd_quant.py)，用 `pytest test_svd_quant.py` 跑（README「测试方法」一节）。这是 README 反复强调的规范：构建产物、性能脚本、对比工具**不要**塞进算子源码目录，正式测试统一放 `tests/amct_ops/`。

#### 4.4.4 代码实践

**实践目标**：把 open project 算子的「定义 → tiling → kernel → 注册」四块源码与公式 \(\text{Out}=X L_1 L_2 + Q(X)Q(R)\) 对上号。

**操作步骤**：

1. 读 [amct_ops/svd_quant/README.md:L11-L38](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_ops/svd_quant/README.md#L11-L38) 的 mermaid 图，记下 `MatMul1`(DownProj)、`MatMul2`(UpProj)、`Quantize`、`MatMul3`(MxFp4) 四个节点。
2. 在 `svd_quant_def.cpp` 里数 `Input(...)` 的个数，确认是 5 个输入（A/W/SC/DP/UP），与公式变量一一对应：A=X，W=R 的 fp4，SC=R 的 scale，DP=L1，UP=L2。
3. 在 `svd_quant_tiling.h` 里数 `TILING_DATA_FIELD_DEF_STRUCT(TCubeTiling, ...)`，确认有 3 个，对应三次 matmul。

**需要观察的现象**：算子的输入个数（5）= 公式里的张量数；tiling 里 cube 子结构数（3）= 公式里的 matmul 次数；def.cpp 里 `AddConfig("ascend950")` 与 README「目标 Ascend950」一致。

**预期结果**：能画出一张表，把 `def.cpp` 的每个 `Input` → 公式变量 → tiling 的 cube 结构三者对齐，说明这个算子的源码组织是「为公式里的每一步配一块代码」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `svd_quant` 的 `def.cpp` 里 `W` 和 `SC` 都是 `DT_UINT8`，而 Python 接口表却写 `float4_e2m1fn_x2` 和 `float8_e8m0fnu`？

> **答**：CANN GE 侧没有原生 4-bit / e8m0 数据类型枚举，所以用 `DT_UINT8` 做存储载体（两个 fp4 元素打包进一个 uint8、e8m0 指数也存成 uint8）。`float4_e2m1fn_x2` / `float8_e8m0fnu` 是 torch_npu 侧的逻辑 dtype，描述语义而非物理存储。这是 open project 算子常见的「物理类型 vs 逻辑类型」分层。

**练习 2**：`svd_quant` 走 open project，所以它多了一个 `op_host` 层。如果删掉 `op_host/svd_quant_tiling.cpp`，构建会怎样？

> **答**：CMakeLists 里 `target_sources(optiling PRIVATE op_host/svd_quant_tiling.cpp)` 会找不到源文件，cmake configure 阶段就报错。即使强行编过，没有 tiling，`aclnnSvdQuantGetWorkspaceSize` 在运行时也无法算出分块，算子无法执行。tiling 是 open project 算子不可省的组件。

---

## 5. 综合实践

**任务**：参照 README「新增算子」一节，为假想的 `lowbit_matmul` 算子规划完整的目录结构与注册方案。

**背景设定**：`lowbit_matmul(A, B) -> C` 做一次「BF16 激活 × INT4 权重」的低比特矩阵乘，需要用 CANN 的 matmul cube、需要按平台算 tiling，目标平台同时覆盖 A2 与 A3。

**步骤**：

1. **判断范式**。它需要 cube matmul + tiling，所以走 open project 范式（参照 `svd_quant`）。
2. **规划目录**。在 `amct_ops/lowbit_matmul/` 下写出完整目录树（含 `op_host/`、`op_kernel/`、`python/lowbit_matmul/csrc/`、`python/lowbit_matmul/__init__.py`、`python/setup.py`、`CMakeLists.txt`、`README.md`），并标注每个文件照搬哪个样板。
3. **算子 IR 草稿**。仿照 `svd_quant_def.cpp`，写出 `lowbit_matmul` 的 `Input/Output` 声明：`A` = `DT_BF16`，`B` = `DT_UINT8`（INT4 打包），`O` = `DT_BF16`，并想清楚 `AICore().AddConfig(<soc>)` 该填哪些 soc。
4. **tiling 草稿**。仿照 `svd_quant_tiling.h`，写出 `LowbitMatmulTilingData` 该有哪些 `TILING_DATA_FIELD_DEF`（至少 `M/K/N`）和几个 `TCubeTiling` 子结构（本题只有一次 matmul，应为 1 个）。
5. **注册方案**。写出：
   - schema 声明（**按 README 规范用 `TORCH_LIBRARY_FRAGMENT(amct, m)`**，不要照抄 `svd_quant` 的 `TORCH_LIBRARY`）：
     ```cpp
     TORCH_LIBRARY_FRAGMENT(amct, m) {
         m.def("lowbit_matmul(Tensor a, Tensor b) -> Tensor");
     }
     ```
   - PrivateUse1 + Meta 两份 impl 的宏骨架；
   - Python 侧 `__init__.py` 里的 `torch.ops.load_library("liblowbit_matmul.so")`；
   - 调用名 `torch.ops.amct.lowbit_matmul(a, b)` 与 `from amct_ops.lowbit_matmul import lowbit_matmul`。
6. **构建方案**。说明：因为它是 open project 范式，仅靠「加目录」**不能**自动打包，必须在 `ops_build.sh` 的 `build_op()` 里加一条仿照 `svd_quant` 的特判分支（含平台检查、`-DBUILD_OPEN_PROJECT=ON`、`--target package` 产 `.run`、`bdist_wheel` 编 `.so`、改名搬进 `build/`）。

**预期产出**：一份目录树 + 一份「文件 → 照搬样板 → 关键改动点」对照表 + schema/tiling/注册三段骨架代码 + 一段构建脚本改动说明。

**自检清单**：

- [ ] 选了 open project 范式并给出理由（cube matmul + tiling）。
- [ ] schema 用的是 `TORCH_LIBRARY_FRAGMENT`（不是 `TORCH_LIBRARY`）。
- [ ] PrivateUse1 与 Meta 两份 impl 都写了。
- [ ] 算子名 `lowbit_matmul` 在 `amct` 内唯一。
- [ ] 明确指出本算子需要改 `ops_build.sh`（不能自动发现），与 `hifloat8_cast` 的「免改」形成对比。

> 待本地验证：完整的编译/安装/调用需要在装了 CANN（且对应 soc）的 NPU 机器上跑 `bash ops_build.sh --soc <soc> lowbit_matmul` 验证；本实践在无 NPU 环境下以「产出可评审的设计文档 + 骨架代码」为完成标准。

## 6. 本讲小结

- 新增算子的第一步是**判断范式**：kernel 自包含、elementwise/LUT 类走**直接扩展**（`hifloat8_cast`，目录 `op_kernel`+`op_extension`）；要用 CANN cube/tiling、走 `aclnn` host API 的走 **open project**（`svd_quant`，多出 `op_host` 一层）。
- `ops_build.sh` 的**自动发现**对直接扩展范式成立（有 `CMakeLists.txt` 就编译、有 `python/<pkg>/` 就收集），所以这类新算子**无需改脚本**；但 open project 算子（`svd_quant`）目前是 `build_op()` 里**硬编码特判**的，新增同类算子要照抄一段特判分支。
- 统一打包走「`build_op` 编 `.so` → `collect_op` 汇进 `staging/` → 根 `setup.py` 用 `find_packages(where='staging')` + `BinaryDistribution` 打成 `amct_ops-*.whl`」四步流水线；包入口 `__init__.py` 来自 `ops_init.py`。
- 所有算子必须注册到 **`amct` 命名空间**，标准四件套是 `TORCH_LIBRARY_FRAGMENT`（schema）+ `TORCH_LIBRARY_IMPL(... PrivateUse1 ...)`（NPU 实现）+ `TORCH_LIBRARY_IMPL(... Meta ...)`（形状推导）+ Python `torch.ops.load_library`；`Meta` 不是装饰，是图模式/`torch.compile` 的必需品。
- `svd_quant` 是 open project 范式样板：`op_host` 写算子 IR（`OpDef`+`OP_ADD`）和 tiling（`BEGIN_TILING_DATA_DEF`+`REGISTER_TILING_DATA_CLASS`+`SvdQuantTiling` 类），`op_kernel` 写 Ascend C cube matmul，`python/csrc` 用 `EXEC_NPU_CMD_V1(aclnnSvdQuant,...)` 调 CANN 侧算子；它锁定 `ascend950`，且必须先装 `.run` 包才能用。
- README 的规范与代码样板有少量出入（如 `svd_quant` 用 `TORCH_LIBRARY` 而非 `FRAGMENT`）；**新增算子一律以 README 为准**，代码样板只作参考。

## 7. 下一步学习建议

- **回到算法侧**：本讲（及 u8-l1/u8-l2）把 `amct_ops` 算子层讲完了。接下来建议回到 u7 系列，看这些算子（尤其是 HiFloat8/MxFp 相关）在量化数据类型 `dtypes/` 里如何被 Python 侧当 `quant_obj` 调用，把「底层算子」与「上层量化」拼成完整闭环。
- **想做实战开发**：若你真要新增一个 NPU 算子，先读 CANN 官方的 [torch_extension 开发指导](https://gitcode.com/cann/ops-nn/blob/master/docs/zh/develop/torch_extension_develop_guide.md)（README 末尾给的参考链接），再以 `hifloat8_cast`（直接扩展）或 `svd_quant`（open project）为脚手架照搬，并配套在 `tests/amct_ops/test_<op>.py` 写精度测试。
- **补 tiling 与 Ascend C 内功**：本讲只讲了 tiling 的「组织方式」与契约，没讲怎么算 UB/核数分块；如需自己写 cube matmul 算子，需进一步学习 CANN 的 `MultiCoreMatmulTiling` / `TCubeTiling` 用法（`svd_quant_tiling.cpp` 是很好的真实案例，358 行覆盖了校验、读形状、BF16/Mx 两种 tiling 计算全流程）。
