# 仓库公共设施：common/includes 与 common/utility

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 `common/` 目录的完整清单，并指出其中哪些部分在 uBench 的日常构建中**真正被执行**（活跃设施），哪些是从 Xilinx Vitis 示例仓库整体继承而来、在本仓库中**处于休眠状态**的设施。
2. 解释 `.mk` 片段机制：一个公共库（如 xcl2）如何通过 `<库名>_SRCS` / `<库名>_CXXFLAGS` / `<库名>_LDFLAGS` 三类变量，被任意示例工程的 Makefile `include` 后注入主机编译命令。
3. 走通「文档生成工具链」：`description.json`（示例元数据）→ `makegen.py` 生成 Makefile/utils.mk/ini，`readme_gen.py` 生成 README.md，以及 `update_*` / `check_*` 脚本族如何维护整个示例仓库的一致性。
4. 拿出证据判断哪些公共库是 Xilinx Vitis 示例仓库的通用件、哪些与本仓库无关，从而在二次开发时知道「哪些可以删、哪些必须留」。

本讲是 advanced 层的「仓库考古」课：我们不新增任何内存系统知识，而是把 u1-l2 里一笔带过的 `common/` 目录彻底拆开。理解它，你才能安全地做 u7-l2 的「自建微基准」实战。

## 2. 前置知识

- **Make 变量的两段式组合**：Makefile 里 `HOST_SRCS += $(xcl2_SRCS)` 这种写法之所以成立，是因为 `include` 另一个文件仅仅等于把那个文件的文本原地展开。所以 `.mk` 片段不是什么特殊机制，就是「把变量定义写在另一个文件里，再 include 进来」。
- **递归展开变量**：`B_TEMP = \`...\`` 这类反引号写法是 shell 命令替换，Make 在变量被使用时才执行它——本讲会看到一个公共脚本正是靠这个机制被「悄悄」挂进每次构建。
- **单一代码源（single source of truth）思想**：Vitis 示例仓库用 `description.json` 描述一个示例（名字、简介、平台、内核、启动参数），再由生成器派生出 Makefile、README 等人造文件。这是「元数据 → 生成物」的经典工程模式，与 u5 讲过的 auto_collect「config.py → 五件套」是同一个思想的两次实现。
- **本讲不要求**安装 Vitis：所有实践都是只读的源码走读加脚本运行，`make -n` 类验证标注了「待本地验证」。

## 3. 本讲源码地图

| 文件/目录 | 作用 | 本讲用法 |
|---|---|---|
| `common/includes/xcl2/` | Xilinx 主机端 C++ 封装库（设备发现、xclbin 读取、仿真判断） | 唯一被 uBench 主机代码使用的库，精读 |
| `common/includes/xcl2/xcl2.mk` | xcl2 库的 Make 变量片段（4 行） | `.mk` 机制的样板 |
| `common/includes/opencl/opencl.mk` | OpenCL 头文件与链接库路径片段 | 环境相关变量注入 |
| `common/includes/{cmdparser,logger,oclHelper,bitmap,lodepng,simplebmp}/` | 命令行解析、日志、OpenCL 错误帮助、三种图像库 | 清点并确认「零引用」 |
| `common/utility/readme_gen/readme_gen.py` | description.json → README.md 生成器 | 精读 + 实际运行 |
| `common/utility/makefile_gen/makegen.py` | description.json → Makefile + utils.mk + ini + xrt.ini 生成器 | 精读关键函数 |
| `common/utility/makefile_gen/descgen.py` | 简写元数据 → 标准 description.json 规整器 | 走读 |
| `common/utility/readme_gen/update_all_readme.sh` 等 `update_*`/`check_*` | 全仓库批量再生成与一致性检查 | 走读 |
| `common/utility/parse_platform_list.py` | 从 `PLATFORM_REPO_PATHS` 解析平台路径 | 唯一在构建期被调用的 utility 脚本 |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile` | 消费 `.mk` 片段的样板工程 | 精读 include 段 |

## 4. 核心概念与源码讲解

### 4.1 公共库清单：common/includes 的七个居民

#### 4.1.1 概念说明

`common/includes/` 下共七个库目录。判断一个库「是否活着」的标准不是它写得多精美，而是**仓库里有没有代码引用它**。用这个标准做一次人口普查，会发现七个库里只有两个（xcl2、opencl）在 uBench 的构建链路上，其余五个是搭 Vitis 示例仓库便车一起搬来的「沉睡房客」。

#### 4.1.2 核心流程

盘点方法（一条 grep 就够）：

1. 对每个库的头文件名在全仓库做 `#include` 检索；
2. 对每个 `.mk` 片段名在各工程 Makefile 里做 `include` 检索；
3. 交叉得到「活跃 / 休眠」结论，列成下表。

盘点结果：

| 库目录 | 内容 | 在 uBench 中的状态 |
|---|---|---|
| `xcl2/` | 主机端封装：设备发现、xclbin 读取、仿真判断、对齐分配器 | **活跃**：16 个 `host.cpp` 包含 `xcl2.hpp`，16 个 Makefile include `xcl2.mk` |
| `opencl/`（只有 .mk，无源码） | OpenCL 头文件路径与 `-lOpenCL -lpthread` 链接选项 | **活跃**：每个数据中心/案例工程 Makefile 都 include 它 |
| `cmdparser/` | 命令行参数解析器 `cmdlineparser` | 休眠：`ubench/`、`case_study/` 下零引用 |
| `logger/` | 彩色终端日志 | 休眠：零引用 |
| `oclHelper/` | OpenCL 错误码翻译 | 休眠：零引用 |
| `bitmap/`、`lodepng/`、`simplebmp/` | 三种图像文件读写库（Vitis 图像类示例用） | 休眠：零引用（uBench 不处理图像） |

为什么会有五个零引用的库？因为整个 `common/` 目录是在初始提交 `2235d89`（"Added Source for uBench and Case_study Benchmarks"）中从 Xilinx `Vitis_Accel_Examples` 仓库**整体搬运**进来的，搬运时没有裁剪。`common/utils.mk` 与各工程目录下的 `utils.mk` 逐字节相同（本讲用 `diff` 复核过）也是同一原因。

#### 4.1.3 源码精读

活跃库 xcl2 的头文件 declares 了 uBench 全部 16 个主机程序共用的骨架设施：

[common/includes/xcl2/xcl2.hpp:40-46](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.hpp#L40-L46) 定义 `OCL_CHECK` 宏：先执行 OpenCL 调用，失败则打印文件行号与错误码并 `exit`。u2-l2 讲过的主机骨架里每一句 OpenCL 调用都被它包裹。

[common/includes/xcl2/xcl2.hpp:61-76](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.hpp#L61-L76) 定义 `aligned_allocator`：用 `posix_memalign(&ptr, 4096, ...)`（第 68 行）把主机缓冲对齐到 4096 字节页边界。头文件第 52-60 行的注释解释了动机——`CL_MEM_USE_HOST_PTR` 只有在指针页对齐时才能零拷贝，否则 XRT 运行时会自建影子缓冲并多出一次 memcpy。这正是 u2-l2 讲过的零拷贝前提的实现处。

[common/includes/xcl2/xcl2.hpp:78-84](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.hpp#L78-L84) 声明 `xcl` 命名空间的四个自由函数：`get_xil_devices`（枚举 Xilinx 设备）、`get_devices`（按厂商过滤）、`read_binary_file`（读 xclbin 到字节向量）、`is_emulation`/`is_hw_emulation`（探测 `XCL_EMULATION_MODE` 环境变量）、`is_xpr_device`（识别 XPR 实验器件）。第 85-104 行还有一个 `Stream` 类，用 `clGetExtensionFunctionAddressForPlatform` 动态加载 Xilinx 的流扩展 API——uBench 未使用，但它是 `hls::stream` 主机侧对偶接口的痕迹。

实现文件给出两个关键函数的身体：

[common/includes/xcl2/xcl2.cpp:36-64](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.cpp#L36-L64) `get_devices`：遍历所有 OpenCL 平台，按平台名匹配厂商（`get_xil_devices` 传 `"Xilinx"`），找不到直接 `exit(EXIT_FAILURE)`；找到则返回该平台下所有 `CL_DEVICE_TYPE_ACCELERATOR` 设备。uBench 主机 `host.cpp` 里「逐卡灌 xclbin」的第一步就在这里。

[common/includes/xcl2/xcl2.cpp:66-85](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.cpp#L66-L85) `read_binary_file`：先用 `access(..., R_OK)` 检查文件可读（不可读则提示「please build」并退出），再用 `ifstream` 把 xclbin 整体读入 `vector<unsigned char>`。第 87-103 行的 `is_emulation`/`is_hw_emulation` 就是 u2-l2 讲过的主机 bank 分支所调用的环境探测器。

#### 4.1.4 代码实践

1. **实践目标**：亲手完成上面的「人口普查」，验证活跃/休眠结论。
2. **操作步骤**（在仓库根目录执行，全部只读）：
   - `grep -rl 'xcl2.hpp' --include='host.cpp' . | wc -l` 统计包含 xcl2 的主机程序数；
   - `grep -rl 'cmdlineparser\|logger.h\|oclHelper\|lodepng\|simplebmp\|bitmap.h' ubench/ case_study/` 检索其余五个库的引用；
   - `grep -rl 'common/includes/xcl2/xcl2.mk' --include=Makefile . | wc -l` 统计 include xcl2.mk 的 Makefile 数。
3. **需要观察的现象**：第一条命令输出 16（8 个数据中心微基准 + 4 个 KNN + 4 个 SpMV 的 host.cpp，另有 auto_collect 生成器在模板字符串里也含此 include）；第二条命令**无任何输出**（退出码 1）；第三条输出 16。
4. **预期结果**：与 4.1.2 表格一致。注意嵌入式（embedded）工程的 host.cpp 不含 `xcl2.hpp`——u4-l3 讲过它们改用自带的 `host.h`，这解释了为何是 16 而不是全量。

#### 4.1.5 小练习与答案

**练习 1**：`opencl/` 目录下只有 `opencl.mk` 一个文件，没有任何 .cpp/.h，为什么它算「活跃库」？
**答案**：它的作用不是提供源码，而是提供**构建变量**：OpenCL 头文件的 include 路径和 `-lOpenCL -lpthread` 链接选项。每个工程 Makefile 第 47、53-54 行都消费它的 `opencl_CXXFLAGS`/`opencl_LDFLAGS`，所以它在每次主机编译中都实际生效。

**练习 2**：如果要给仓库瘦身，`common/includes/` 下哪些目录可以安全删除？删除前还应做什么检查？
**答案**：`cmdparser`、`logger`、`oclHelper`、`bitmap`、`lodepng`、`simplebmp` 六个目录当前零引用，可删。但删除前应 grep 确认没有 Makefile include 它们的 `.mk`（当前也没有），并确认未来不打算引入命令行参数或图像处理功能——它们是「备用件」而非「废件」。

**练习 3**：`xcl2.hpp` 第 33-37 行的一串 `#define CL_HPP_...` 宏起什么作用？
**答案**：它们在包含 `CL/cl2.hpp`（OpenCL C++ 绑定）之前配置其行为：目标 OpenCL 1.2、最小 1.2、允许从数组构造 Program、启用已废弃的 1.2 API。集中放在公共头里，使所有主机程序对 OpenCL 绑定的版本策略一致。

### 4.2 mk 片段机制：库如何被「注射」进任意工程

#### 4.2.1 概念说明

`.mk` 片段解决的问题是：公共库的源码躺在 `common/includes/<lib>/`，而消费它的工程散布在仓库各深度层级。如果每个 Makefile 都手写 `g++ .../xcl2.cpp -I.../xcl2 ...`，路径会随工程深度变化，且库一旦增删源文件就要改几十个 Makefile。Vitis 示例仓库的解法是**命名约定驱动的变量注入**：

- 每个库自带一个 `<lib>.mk`，只定义以库名为前缀的变量：`<lib>_SRCS`（要一起编译的源文件）、`<lib>_HDRS`、`<lib>_CXXFLAGS`（头文件搜索路径）、`<lib>_LDFLAGS`（可选）；
- 工程主 Makefile `include` 该片段后，用 `+=` 把这些变量并入自己的 `HOST_SRCS`/`CXXFLAGS`/`LDFLAGS`；
- 由于路径全部以 `$(ABS_COMMON_REPO)` 绝对化（`readlink -f` 消解相对路径），工程无论多深都能引用。

#### 4.2.2 核心流程

一次主机编译的变量汇聚流：

```text
工程 Makefile
  ├─ COMMON_REPO = ../../../../../          （相对上溯到仓库根）
  ├─ ABS_COMMON_REPO = $(shell readlink -f $(COMMON_REPO))
  ├─ include $(ABS_COMMON_REPO)/common/includes/opencl/opencl.mk   → 定义 opencl_CXXFLAGS / opencl_LDFLAGS
  ├─ include $(ABS_COMMON_REPO)/common/includes/xcl2/xcl2.mk      → 定义 xcl2_SRCS / xcl2_HDRS / xcl2_CXXFLAGS
  ├─ CXXFLAGS += $(xcl2_CXXFLAGS) $(opencl_CXXFLAGS) -Wall -O0 -g -std=c++11
  ├─ LDFLAGS  += $(opencl_LDFLAGS)          （xcl2_LDFLAGS 是空变量，见下）
  ├─ HOST_SRCS += $(xcl2_SRCS)              ← 公共库源码在此注入
  ├─ HOST_SRCS += src/host.cpp src/krnl_config.h
  └─ $(EXECUTABLE): $(HOST_SRCS)
       $(CXX) $(CXXFLAGS) $(HOST_SRCS) -o ubench $(LDFLAGS)
```

关键点：`xcl2.cpp` 从不出现在任何工程的 `src/` 里，但通过 `HOST_SRCS += $(xcl2_SRCS)`，它和 `src/host.cpp` 被同一条 g++ 命令编译链接——这就是「注射」。

#### 4.2.3 源码精读

xcl2.mk 全文只有 4 行有效内容：

[common/includes/xcl2/xcl2.mk:1-4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/xcl2/xcl2.mk#L1-L4) 定义 `xcl2_SRCS`（指向 xcl2.cpp 绝对路径）、`xcl2_HDRS` 和 `xcl2_CXXFLAGS`（`-I` 到 xcl2 目录，使 `#include "xcl2.hpp"` 可解析）。**注意它没有定义 `xcl2_LDFLAGS`**。

消费端在样板工程 Makefile 的 include 段：

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile:28-37](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L28-L37) 第 29 行 `COMMON_REPO = ../../../../../` 五级上溯到仓库根，第 31 行用 `readlink -f` 绝对化；第 37 行先 include 工程**本地**的 `./utils.mk`（环境守门与工具函数，u1-l3 精读过）。

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile:46-56](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L46-L56) 第 47-48 行 include 两个库片段；第 49-51 行把 `xcl2_CXXFLAGS` 并入 `CXXFLAGS`、`xcl2_SRCS` 并入 `HOST_SRCS`；第 53-54 行并入 `opencl_CXXFLAGS`/`opencl_LDFLAGS`；第 56 行追加工程自己的 `src/host.cpp`（以及作为依赖列出的 `src/krnl_config.h`）。

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile:103-104](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L103-L104) 主机链接规则：`$(CXX) $(CXXFLAGS) $(HOST_SRCS) ... -o ubench $(LDFLAGS)`。展开后 g++ 命令行里同时出现 `common/includes/xcl2/xcl2.cpp` 与 `src/host.cpp`——注射完成。

环境相关的 opencl.mk 则展示了片段如何隔离平台差异：

[common/includes/opencl/opencl.mk:2-15](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/includes/opencl/opencl.mk#L2-L15) 根据 `HOST_ARCH` 决定 XRT 路径：x86 用 `$(XILINX_XRT)`，交叉编译（aarch64）改用 `$(SYSROOT)/usr/` 且头文件路径多一层 `xrt`；最后产出 `opencl_CXXFLAGS`（含 Vivado include）与 `opencl_LDFLAGS`（`-lOpenCL -lpthread`）。u4-l3 的 ZCU104 交叉编译正是靠这段切换到 sysroot 里的头文件与库。

**两个值得记录的细节**：

1. **幽灵变量 `xcl2_LDFLAGS`**：Makefile 第 50 行写着 `LDFLAGS += $(xcl2_LDFLAGS)`，但 xcl2.mk 根本没定义这个变量——GNU Make 对未定义变量展开为空串，所以这行是无害的空操作。它是模板复制粘贴的化石，且被 u5 的 auto_collect 生成器 `makefile_gen.py` 原样复制进每个生成 Makefile（`ubench/offchip_bandwidth/datacenter/auto_collect/makefile_gen.py` 第 59 行）。读懂它而不是「修复」它，是维护这类生成代码仓库的重要素养。
2. **唯一活跃的 utility 脚本**：[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk:14](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk#L14) 的 `B_TEMP` 反引号里调用了 `$(ABS_COMMON_REPO)/common/utility/parse_platform_list.py $(DEVICE)`——也就是说，每次构建展开 `B_NAME` 时都会执行这个 13 行的小脚本。它在 [common/utility/parse_platform_list.py:5-13](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/parse_platform_list.py#L5-L13) 里遍历 `PLATFORM_REPO_PATHS` 环境变量中的目录，找到含指定平台名的那个并打印，用于非 xpfm 形式的 DEVICE 名解析。

#### 4.2.4 代码实践

1. **实践目标**：解释 xcl2.mk 如何把 `xcl2.cpp` 注入 `HOST_SRCS` 并传递 `xcl2_CXXFLAGS`，并用证据验证（不装 Vitis）。
2. **操作步骤**：
   - 静态验证：通读 `common/includes/xcl2/xcl2.mk`（4 行）与样板 Makefile 第 46-56、103-104 行，画出 4.2.2 的汇聚图；
   - 动态验证（无需 Vitis）：`XILINX_VITIS` 未设时 utils.mk 会在解析期报错，所以先给个假值再做空跑：
     `make -C ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit -n exe XILINX_VITIS=/tmp/fake DEVICE=xilinx_u200_xdma_201830_1`
     `-n` 只打印命令不执行；
   - 在打印出的 g++ 命令里搜索 `xcl2.cpp` 与 `-I.../xcl2`。
3. **需要观察的现象**：打印的命令形如 `g++ -I<仓库根>/common/includes/xcl2 -I.../include -Wall -O0 -g -std=c++11 ... <仓库根>/common/includes/xcl2/xcl2.cpp src/host.cpp src/krnl_config.h -o ubench -L.../lib -lOpenCL -lpthread ...`。
4. **预期结果**：`xcl2.cpp` 出现在命令行（证明 SRCS 注入），`-I` 含 xcl2 目录（证明 CXXFLAGS 注入），而 `$(xcl2_LDFLAGS)` 展开处没有任何 xcl2 相关的 `-L/-l`（证明幽灵变量为空）。本沙盒环境无法执行 make，此步为**待本地验证**；静态部分的结论已由源码逐行核实。

#### 4.2.5 小练习与答案

**练习 1**：仿照 xcl2.mk，为假想的 `common/includes/mylib/`（含 mylib.cpp/mylib.h）写一个 mylib.mk。
**答案**（示例代码）：
```make
mylib_SRCS:=${COMMON_REPO}/common/includes/mylib/mylib.cpp
mylib_HDRS:=${COMMON_REPO}/common/includes/mylib/mylib.h
mylib_CXXFLAGS:=-I${COMMON_REPO}/common/includes/mylib
```
工程端再 `include` 它并三行 `+=` 即可。

**练习 2**：为什么 `.mk` 片段里路径用 `${COMMON_REPO}`（相对）而后由工程侧 `readlink -f` 绝对化，而不是片段自己写绝对路径？
**答案**：仓库会被 clone 到任意位置，片段无法预知绝对路径；统一以 `COMMON_REPO` 为锚点、在工程侧一次性绝对化，使所有深度的工程共享同一套片段，且 `make -C` 从别的目录调用时路径依然正确。

**练习 3**：如果把 xcl2.mk 里的变量改名为 `SRCS`/`CXXFLAGS`（去掉前缀），会发生什么？
**答案**：片段 include 进来后直接改写全局 `SRCS`/`CXXFLAGS`，与工程自身及其他片段的同名变量互相踩踏（尤其 `+=` 的叠加顺序会变得不可控）。库名前缀就是命名空间，是这套机制能容纳任意多库的原因。

### 4.3 文档生成工具链：description.json 与它的生成器家族

#### 4.3.1 概念说明

Vitis 示例仓库的核心工程决策是：**每个示例目录放一份 `description.json` 元数据，其余人造文件（Makefile、utils.mk、连接 ini、xrt.ini、README.md）全部由脚本从它生成**。这样上百个示例的构建与文档保持机械一致。生成器家族住在 `common/utility/`：

| 脚本 | 输入 → 输出 | 作用 |
|---|---|---|
| `makefile_gen/makegen.py` | description.json → Makefile、utils.mk、`<容器名>.ini`、xrt.ini | 单示例构建文件生成器 |
| `makefile_gen/descgen.py` | 简写键名的 description → 标准-description.json | 元数据规整器 |
| `readme_gen/readme_gen.py` | description.json → README.md | 单示例文档生成器 |
| `readme_gen/gs_summary*.py`、`create_catalog.py`、`md2rst/md2rst.py` | 各示例 description.json → 汇总索引/rst | 全仓库目录页生成 |
| `update_makegen_all.sh`、`update_all_readme.sh`、`update_descgen_all.sh`、`update_md2rst_all.sh` | git ls-files 找到全部 description.json | 批量再生成 |
| `check_makefile.sh`、`check_readme.sh`、`check_descr.py`、`check_json.py`、`check_license.sh`、`check_target_device.py` | 同上 | 一致性巡检（再生成后 diff） |
| `build_what.sh`、`device_list.py`、`Consolidation.py` | git 变更/全部示例 | CI 增量构建决策、设备清单、示例打包 |

**关键事实**：uBench 仓库里**一份 description.json 都不存在**——`find` 全树无结果，`git log --all` 也从未有过该文件的增删记录。因此这整条工具链在 uBench 中处于休眠：工具搬来了，燃料没搬。但它的「接线」还留在现场：每个工程 `utils.mk` 末尾仍有 `docs:` 目标（见下文），Makefile 的 `.PHONY` 行里也仍列着 `docs`。反过来说，uBench 自己的自动化是另一套独立实现——u5 讲的 `auto_collect`（config.py + 四个 `*_gen.py`），两者思想同源、代码无共享。

#### 4.3.2 核心流程

设计期的生成闭环（在原 Vitis 示例仓库中）：

```text
手写 description.json（示例元数据：名称/简介/平台/主机/内核/启动参数）
   ├─ makegen.py  → Makefile + utils.mk + <容器>.ini（sp/slr/nk）+ xrt.ini
   ├─ readme_gen.py → README.md
   └─ update_*_all.sh（CI/批量）→ 遍历 git ls-files 中的 description.json 重新生成
check_* 巡检：临时挪走现文件 → 再生成 → diff，不一致即 FAIL
```

防手工篡改机制：description.json 里可写 `"match_makefile": "false"` / `"match_readme": "false"`，生成器见到即拒绝覆盖（承认这是手工维护区）；不加此字段就意味着「此文件是生成物，不许手改」。

#### 4.3.3 源码精读

**readme_gen.py——README 的诞生**。主流程在文件尾部：

[common/utility/readme_gen/readme_gen.py:124-147](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/readme_gen/readme_gen.py#L124-L147) 第 125 行从命令行取 description.json 路径；第 135 行 `assert("OpenCL" in data['runtime'])` 强制只服务 OpenCL 示例；第 137-139 行检查 `match_readme` 逃生门；第 141 行打开 `README.md`（**写死为当前目录**，所以必须在示例目录内运行）；第 142-145 行依次调用四个章节函数。注意第 147 行 `target.close` 少了括号——文件对象从未显式关闭，靠 CPython 引用计数兜底，是典型的休眠 bug。

四个章节函数：

- [common/utility/readme_gen/readme_gen.py:11-56](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/readme_gen/readme_gen.py#L11-L56) `overview()`：写示例名、下划线标题、`description` 段落，可选 `more_info`、`perf_fields`（性能表格）、`key_concepts`、`keywords`；
- [common/utility/readme_gen/readme_gen.py:58-77](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/readme_gen/readme_gen.py#L58-L77) `requirements()`：把 `ndevice`/`device` 列表渲染成「排除/支持平台」两节——uBench 各 README 里「不支持 samsung/zc 板卡」的 make 检查正源于此约定；
- [common/utility/readme_gen/readme_gen.py:79-99](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/readme_gen/readme_gen.py#L79-L99) `hierarchy()`：第 86 行用 `subprocess` 执行 `git ls-files | grep -e data -e src`，把 **git 索引里的文件清单**嵌进 README 的 DESIGN FILES 节——所以生成的 README 永远与仓库实际文件同步；第 93 行 `if flag is 1:` 在 Python 3.8+ 会触发 SyntaxWarning（`is` 比较字面量），但仍可运行；
- [common/utility/readme_gen/readme_gen.py:101-119](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/readme_gen/readme_gen.py#L101-L119) `commandargs()`：把 `launch[0].cmd_args` 做 `BUILD/`→`<`、`PROJECT`→`.`、`.xclbin`→` XCLBIN>` 的替换后渲染成运行命令。

另一个实操要点在 [common/utility/readme_gen/readme_gen.py:1](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/readme_gen/readme_gen.py#L1)：shebang 指向 Xilinx 内部机器路径 `/tools/cpkg/.packages/x86_64/RHEL7.2/python/3.7.1/bin/python3.7`，在任何其他机器上直接 `./readme_gen.py` 都会失败——必须显式 `python3 readme_gen.py` 调用。这本身就是「从 Xilinx 内网仓库搬出」的直接物证。

**makegen.py——Makefile 的诞生，以及 ubench.ini 三指令的祖先**。

[common/utility/makefile_gen/makegen.py:638-655](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/makefile_gen/makegen.py#L638-L655) `create_mk` 按序调用各分节函数拼出 Makefile；`create_utils` 拼出 utils.mk——其中 [common/utility/makefile_gen/makegen.py:630-635](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/makefile_gen/makegen.py#L630-L635) 的 `readme_gen()` 正是把 `docs: README.md` / `README.md: description.json` 两条规则写进 utils.mk 的源头。对照 [ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk:89-92](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/utils.mk#L89-L92) 可见逐字符相同的成品——这就是各工程 utils.mk 尾巴上 `docs` 目标的来历，也是「这些 Makefile/utils.mk 当年由该工具生成」的指纹。

[common/utility/makefile_gen/makegen.py:55-99](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/makefile_gen/makegen.py#L55-L99) 是 4.2 命名约定的生成器侧证据：`add_includes1` 对 description.json 的每个 `includepaths` 项写 `include $(ABS_COMMON_REPO)/<路径>/< basename>.mk`；`add_includes2` 用 basename 拼出 `$(<lib>_CXXFLAGS)`、`$(<lib>_LDFLAGS)`、`$(<lib>_SRCS)` 三行 `+=`。也就是说，「库名前缀变量」契约由生成器与片段两端共同维护。

[common/utility/makefile_gen/makegen.py:657-683](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/makefile_gen/makegen.py#L657-L683) `create_config()`：若某加速器声明了 `compute_units`/`num_compute_units`，则创建 `[connectivity]` 段并逐计算单元写 `sp=<内核>_<序号>.<参数>:<memory>`、`slr=<内核>_<序号>:<SLR>`、`nk=<内核>:<数量>`。**这正是 u3-l3 精读过的 ubench.ini 三指令的祖先**——uBench 手写的 ubench.ini 沿用了同一套 v++ 连接语法。

[common/utility/makefile_gen/makegen.py:685-717](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/makefile_gen/makegen.py#L685-L717) 主流程：第 690-696 行受 `match_ini` 门槛控制生成 `xrt.ini`（`[Debug] profile=true`）；第 707-715 行受 `match_makefile` 门槛生成 Makefile 与 utils.mk。

**血脉鉴定**：[common/utility/makefile_gen/makegen.py:13-23](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/makefile_gen/makegen.py#L13-L23) 的 `create_params` 在第 17 行执行 `dirNameList.index("Vitis_Accel_Examples")`——用当前目录路径中必须出现 `Vitis_Accel_Examples` 组件来推算 `COMMON_REPO` 上溯级数。**在 uBench 目录下运行必然抛 `ValueError`**（路径里没有这个组件），即该脚本原样不可用；这也意味着 uBench 各 Makefile 里的 `COMMON_REPO` 级数是搬运后手工校准的（案例研究三级、微基准五级、auto_collect 生成版六级——u5-l2 讲过后者）。加之第 1 行 shebang 是 `#!/usr/bin/env python`（Python 2 时代写法），双重印证「整体继承、未做适配」。

**批量与巡检**：

[common/utility/readme_gen/update_all_readme.sh:8-25](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/readme_gen/update_all_readme.sh#L8-L25) 第 8 行用 `git ls-files | grep 'description.json'` 枚举所有含元数据的示例目录，循环内第 16 行 grep `"match_readme": "false"` 跳过手工区，否则 `rm README.md` 后 `make docs` 再生成。在 uBench 中第 8 行结果为空列表，整个脚本空转——休眠的机制性原因。

[common/utility/check_makefile.sh:29-35](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/check_makefile.sh#L29-L35) 巡检逻辑的精髓：把现役 Makefile/utils.mk 改名暂存，调 makegen.py 重新生成，再 `diff` 新旧——生成物与手改的任何偏差都会被抓住（「文件是生成物」纪律的执法者）。

**规整器 descgen.py**：

[common/utility/makefile_gen/descgen.py:16-23](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/makefile_gen/descgen.py#L16-L23) 把简写键（`example`/`overview`/`board`/`nboard`）映射为标准键（`name`/`description`/`device`/`ndevice`）；[common/utility/makefile_gen/descgen.py:68-74](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/common/utility/makefile_gen/descgen.py#L68-L74) 把 `libs` 列表展开为 `REPO_DIR/common/includes/<lib>` 形式的 `includepaths`；最后在第 110-111 行写回 description.json。它是手写元数据与生成器之间的适配层。

#### 4.3.4 代码实践

1. **实践目标**：走通 description.json → README.md 的生成流程，理解每个章节从哪个 JSON 键来；并为一个 uBench 微基准补写一份 description.json。
2. **操作步骤**：
   - 选一个样板目录复制到沙盒（**不要在仓库内添加文件**），例如 `cp -r ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit /tmp/rgtest`（若在 /tmp 无权限，复制到你自己的 fork 里也行）；
   - 在该目录写一份最小 description.json（**示例代码**）：
     ```json
     {
       "name": "read_DDR_2ports_512bit",
       "description": ["Off-chip read bandwidth microbenchmark: 2 concurrent 512-bit ports, DDR, 300 MHz."],
       "runtime": ["OpenCL"],
       "host": {"host_exe": "ubench"},
       "platform_type": "pcie",
       "key_concepts": ["m_axi bandwidth", "read burst"],
       "keywords": ["bandwidth", "DDR"]
     }
     ```
   - 运行（必须显式用 python3，原因见 shebang 分析）：
     `python3 <仓库根>/common/utility/readme_gen/readme_gen.py description.json`
   - 打开生成的 README.md，对照 4.3.3 的四个章节函数逐一找键的来源；
   - 加做一次失败实验：在原仓库某微基准目录执行 `make docs`（需设 `XILINX_VITIS` 与 `DEVICE` 假值才能通过解析期检查）。
3. **需要观察的现象**：生成命令打印 `VITIS README File Genarator`（原文即拼错为 Genarator）与 `Generating the README for ...`；README.md 含标题、下划线、description、KEY CONCEPTS/KEYWORDS 行，以及 `## DESIGN FILES` 节内嵌的 `git ls-files` 输出（在拷贝目录里 git 索引不存在，该节可能为空或报子进程错误——这本身就是「hierarchy() 依赖 git 仓库」的证据）。`make docs` 实验预期报 `No rule to make target 'description.json', needed by 'README.md'` 类错误（**待本地验证**）。
4. **预期结果**：四章节结构与 4.3.3 源码逐条对上；从而理解 uBench 现存各 README 是**手工维护**的（因为 description.json 缺失，`match_readme` 门槛与 docs 目标均无从生效）。

#### 4.3.5 小练习与答案

**练习 1**：uBench 各微 benchmark 目录的 README 是谁生成的？
**答案**：不是 readme_gen.py 生成的。仓库中不存在任何 description.json，readme_gen 没有输入；且这些 README 的内容（参数表、修改指南）超出了 readme_gen 能渲染的章节集合。它们是作者手工撰写的，utils.mk 尾部的 docs 规则只是生成器时代的遗留接线。

**练习 2**：`update_all_readme.sh` 为什么在 uBench 中是安全的空转？如果某天有人在某目录添加了 description.json，会发生什么？
**答案**：脚本第 8 行以 `git ls-files | grep description.json` 枚举目标，uBench 中结果为空，循环体不执行。一旦有人添加了 description.json，脚本会进入该目录 `rm README.md` 再 `make docs`——把手工 README 覆盖为生成版。所以复活这条工具链前必须先评估现有手工文档是否要保留（或在 json 里写 `"match_readme": "false"`）。

**练习 3**：给出至少三条「common/ 继承自 Vitis_Accel_Examples」的源码级证据。
**答案**：(a) makegen.py 第 17 行 `dirNameList.index("Vitis_Accel_Examples")` 硬编码原仓库名；(b) readme_gen.py 第 1 行 shebang 指向 Xilinx 内网 RHEL7.2 的 python3.7 绝对路径；(c) 所有库源文件带 Xilinx 版权的 BSD 三条款头，且 `common/` 全目录在 uBench 首次源码提交中一次性出现；(d) md2rst.py 的 DEVICES 表全是 2019.2 时代平台（zcu102_base、xilinx_u200_qdma 等）。

## 5. 综合实践

**任务：为 uBench 复活一条「迷你文档流水线」，并用它检验仓库现状。**

1. 在你自己的 fork 中选 `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit`，为它撰写一份尽可能完整的 description.json：`name`/`description` 用该目录 README 的真实信息；`runtime` 必须含 `"OpenCL"`（否则 assert 失败）；在 `host` 里写 `host_exe`（该工程可执行名是 `ubench`）与 `launch[0].cmd_args`（模仿 `<BUILD>/ubench.xclbin` 的写法，观察 commandargs() 如何把 `BUILD/` 翻译成 `<`）；尝试 `perf_fields`/`performance` 渲染一张带宽表。
2. 用 `python3 common/utility/readme_gen/readme_gen.py description.json` 生成 README.md，与仓库手工 README.md 做 diff，列出生成器**表达不了**的内容（如五参数修改指南）——这解释了作者为何放弃元数据方案手写文档。
3. 用 4.2.4 的 `make -n` 方法截获该工程完整 g++ 命令行，标注：哪些片段来自 xcl2.mk、哪些来自 opencl.mk、哪一段是幽灵 `xcl2_LDFLAGS` 的空展开、`src/krnl_config.h` 为何出现在命令行却没有产出目标（提示：它只作为依赖触发重编，g++ 会把它当头文件一并预处理）。
4. 产出一份一页的《common/ 取舍备忘》：若做二次开发发布，保留 `xcl2`、`opencl`、`utils.mk`、`parse_platform_list.py`；可删六个休眠库；`utility/` 下生成器家族按是否打算恢复元数据驱动决定去留。每条给出本讲引用过的证据行号。

## 6. 本讲小结

- `common/includes/` 七个库中只有 xcl2（16 个 host.cpp 引用）与 opencl.mk（16 个 Makefile include）在 uBench 构建中活跃，其余六个是 Vitis 示例仓库整体搬运带来的零引用件。
- `.mk` 片段机制 = 命名空间化的 `<lib>_SRCS/_CXXFLAGS/_LDFLAGS` 变量 + 工程 Makefile 的 `include`/`+=` 两步注射；生成器 makegen.py 的 `add_includes1/2` 与片段两端共同维护这一契约。
- 所有数据中心 Makefile 第 50 行的 `$(xcl2_LDFLAGS)` 是从未定义的幽灵变量（展开为空），被 auto_collect 生成器一并复制——读懂化石比随手「修复」更重要。
- 文档生成工具链以 description.json 为单一代码源：makegen.py 生成 Makefile/utils.mk/ini/xrt.ini（其 `create_config` 是 ubench.ini 三指令 sp/slr/nk 的祖先），readme_gen.py 生成 README.md（内嵌 `git ls-files` 文件清单），update_*/check_* 族负责批量再生成与 diff 巡检。
- uBench 中不存在任何 description.json，整条工具链休眠；utils.mk 尾部的 `docs` 目标与 Makefile `.PHONY` 里的 `docs` 是其仅存接线；uBench 自己的自动化是独立的 auto_collect 体系（u5）。
- 血脉证据：makegen.py 硬编码 `Vitis_Accel_Examples` 路径组件（在 uBench 内运行必抛 ValueError）、readme_gen.py 的 Xilinx 内网 shebang、2019.2 平台表——`common/` 是未做适配的整体继承。

## 7. 下一步学习建议

- 下一讲 **u7-l2（创建你自己的微基准）**：本讲清点的「五件套 + .mk 片段 + COMMON_REPO 锚点」正是你复用建新基准的全部原料；动手前重读 4.2 的汇聚图。
- 若你关心构建系统的另一半——环境守门与 v++ 编译链——回看 u1-l3 对 utils.mk 各 check 目标的精读；本讲的 `parse_platform_list.py` 是那套检查里唯一的外部脚本依赖。
- 若你想把「元数据 → 生成物」模式用在团队工程里，对比两套实现取长补短：Vitis 的 description.json + makegen.py（声明式 JSON、跨示例一致性强）vs uBench 的 config.py + `*_gen.py`（命令式 Python、表达参数交叉积更直接，u5-l2）。
- 延伸阅读源码：`common/utility/md2rst/md2rst.py`（Markdown→reStructedText，看它如何为文档门户改写链接）与 `common/utility/build_what.sh`（按 git diff 决定 CI 增量重建哪些示例的雏形）。
