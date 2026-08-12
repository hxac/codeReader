# 从零编写自己的 L1 内核

## 1. 本讲目标

前面十几讲里，我们一直在「读」Vitis 加速库别人写好的内核。本讲把角色反过来——你要自己「写」一个符合仓库规范的 L1 流式内核，并让它被工具链与 CI 正确识别。

学完后你应当能够：

1. 独立设计一个符合 `utils` 风格的 L1 模板化头件（命名空间 + 前向声明 + 实现）。
2. 把模板内核包成可综合的 `extern "C"` DUT，并正确处理 end-flag 流。
3. 编写配套的 testbench（test.cpp），用「喂入数据 + 校验输出 + 校验 end flag」三段式判定 PASS/FAIL。
4. 写出可被 `make run TARGET=csim` 驱动的 Makefile、`hls_config.tmpl` 与 `description.json`，并跑通 csim、读懂 csynth 报告。

本讲是一节「把前面所有零散知识焊成一条完整交付物」的总结课。它的全部材料都来自 `utils/L1/tests/stream_dup` 这个最小范本。

## 2. 前置知识

本讲默认你已经掌握以下两讲建立的心智模型，这里只做最简回顾：

- **u3-l2（HLS pragma 如何映射硬件）**：`#pragma HLS pipeline II=1` 决定吞吐（吞吐 = 1/II），`unroll` 用面积换并行，`dataflow` 做任务级流水。本讲的内核只用 `pipeline II=1`。
- **u14-l1（测试基础设施与 CI）**：每个 L1 用例目录是一份机器可读的「身份证」——`description.json` 的 `flow` 字段决定走 HLS 流水线，`topfunction` 指明综合顶层；`hls_config.tmpl` 用 `${VAR}` 占位符，由 Makefile 内嵌 Python 渲染成 `hls_config.cfg`；Makefile 再按大写 TARGET（csim/csynth/…）分派给 `v++ -c` 或 `vitis-run`。

此外你需要记住 u3-l1 的两个约定：

- **hls::stream** 是单向 FIFO，元素只能顺序、单次读写，强制流式访问以映射 II=1 的硬件流水。
- **end-flag 约定**：流不携带长度信息，必须配一条伴生 `hls::stream<bool>` 标记结束；`stream_dup` 采用「前瞻式消费」——对 N 个数据要读 N+1 个 flag（生产者写 N 个 `false` + 1 个 `true`）。

如果你对上述任何一点感到陌生，建议先回看对应讲义。

## 3. 本讲源码地图

本讲把 `stream_dup` 用例当作「可复制的样板房」，涉及五个文件，分属内核侧与用例侧：

| 文件 | 角色 | 本讲用途 |
| --- | --- | --- |
| `utils/L1/include/xf_utils_hw/stream_dup.hpp` | 内核侧模板头件 | 模板化头件的写法范本 |
| `utils/L1/include/xf_utils_hw/common.hpp` | 内核侧公共宏 | `XF_UTILS_HW_STATIC_ASSERT` 编译期护栏 |
| `utils/L1/include/xf_utils_hw/types.hpp` | 内核侧类型头 | `AP_INT_MAX_W` 必须先于 `ap_int.h` 设置 |
| `utils/L1/tests/stream_dup/test.cpp` | 用例侧 testbench + DUT | DUT 封装与 main 分派的范本 |
| `utils/L1/tests/stream_dup/Makefile` | 用例侧构建脚本 | **完全通用**，新用例可逐字复制 |
| `utils/L1/tests/stream_dup/description.json` | 用例侧 CI 身份证 | 元数据字段如何声明 |
| `utils/L1/tests/stream_dup/hls_config.tmpl` | 用例侧 HLS 配置模板 | `syn.top` / `csim.argv` 等占位符 |

一个核心结论先放这里：**新建一个 L1 用例，你只需要提供 test.cpp、hls_config.tmpl、description.json 三个文件；Makefile 可以逐字复制**，因为它不硬编码任何用例名。

## 4. 核心概念与源码讲解

### 4.1 模板化头件设计

#### 4.1.1 概念说明

L1 内核的本质是「一个可复用的 HLS C++ 函数」。为了让它在不同位宽、不同并行度下都能用，`utils` 库一律把它写成**模板函数**，放在 header-only 的 `.hpp` 里。一份合格的模板头件要满足四点：

1. **include 顺序**：先引本库的 `types.hpp`（它负责把 `AP_INT_MAX_W` 设到 4096 再引 `ap_int.h`），再引 `common.hpp`（提供静态断言宏）。
2. **命名空间**：统一收在 `xf::common::utils_hw` 下，避免污染全局。
3. **前向声明 + 实现两段式**：先把模板签名声明出来（带 Doxygen 注释），再在下方给出实现。这种写法对外只暴露签名、对内集中实现，方便读者快速扫接口。
4. **模板参数命名**：类型参数用 `_TIn`、非类型参数用 `_NStrm` 这类带下划线前缀的名字，与全库一致。

#### 4.1.2 核心流程

设计一个新内核头件的流程：

1. 想清楚「输入是什么流、输出是什么流、要不要 end flag」——本库所有流式内核的签名都长成 `(数据流入, end 流入, 数据流出, end 流出)`。
2. 选模板参数：元素类型作为类型参数，路数/宽度作为非类型 `int` 参数。
3. 写前向声明（带注释），再写实现。
4. 在实现里用 `#pragma HLS pipeline II=1` 标注主循环；循环内对每一路输出用 `unroll`。
5. 如有参数约束（如「输出路数不能超过输入路数」），用 `XF_UTILS_HW_STATIC_ASSERT` 在编译期拦截。

#### 4.1.3 源码精读

先看 include 顺序与命名空间开篇。`stream_dup.hpp` 先引类型头与公共头：

[utils/L1/include/xf_utils_hw/stream_dup.hpp:19-20](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L19-L20) —— 引入 `types.hpp`（内含 `ap_int.h` 与 `AP_INT_MAX_W=4096`）与 `common.hpp`（内含静态断言宏），顺序不能反。

`types.hpp` 之所以必须先引，是因为它在引 `ap_int.h` 之前把最大位宽撑到 4096：

[utils/L1/include/xf_utils_hw/types.hpp:75-82](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/types.hpp#L75-L82) —— `#undef` 后把 `AP_INT_MAX_W` 重定义为 4096，**之后**才 `#include "ap_int.h"`；若你的内核用到宽 `ap_int`，不先撑大这个上限会编译失败。

接着看前向声明——对外只暴露签名与 Doxygen 注释，参数含义一目了然：

[utils/L1/include/xf_utils_hw/stream_dup.hpp:46-50](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L46-L50) —— 模板函数 `streamDup<_TIn, _NStrm>` 的声明：一进一出多路，每路各自带 end flag。

最后看实现，这是本讲要仿写的核心范式——「前瞻读 flag + while 循环 + pipeline II=1 + 末尾写结束标志」：

[utils/L1/include/xf_utils_hw/stream_dup.hpp:87-108](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L87-L108) —— `streamDup` 的实现：第 92 行先预读一个 flag，进入 `while(!e)` 循环；循环内第 94 行 `pipeline II=1` 保证每拍处理一个元素，第 98-102 行的 `unroll` 把「写 N 路」展开成 N 个并发写端口；循环退出后第 104-107 行给每路补写一个 `true` 收尾。

另一份重载演示了「编译期护栏」的写法——当模板参数有约束时，用静态断言把误用拦在编译瞬间：

[utils/L1/include/xf_utils_hw/stream_dup.hpp:117](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L117) —— `XF_UTILS_HW_STATIC_ASSERT(_NDStrm <= _NIStrm, ...)`：复制输出路数不能超过输入路数，否则编译期即报错，零运行时代价。

这个宏最终展开成标准的 `static_assert`：

[utils/L1/include/xf_utils_hw/common.hpp:216-220](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/common.hpp#L216-L220) —— C++11 及以上映射到 `static_assert((b), m)`，否则退化为运行时 `assert`；综合时 `__SYNTHESIS__` 分支会把它变成空操作。

#### 4.1.4 代码实践

**实践目标**：仿照 `stream_dup.hpp`，为「把输入流每个元素乘 2」这个功能写一份模板头件骨架。

**操作步骤**：

1. 在 `utils/L1/include/xf_utils_hw/` 下新建 `stream_double.hpp`（示例代码，非项目原有文件）。
2. 照搬三件套：include 守卫、引 `types.hpp`+`common.hpp`、`xf::common::utils_hw` 命名空间。
3. 写前向声明 `template<typename _TIn> void streamDouble(...)`，签名是一进一出各带 end flag。
4. 写实现，主循环用 `#pragma HLS pipeline II=1`。

参考骨架（**示例代码**）：

```cpp
#ifndef XF_UTILS_HW_STREAM_DOUBLE_H
#define XF_UTILS_HW_STREAM_DOUBLE_H

#include "xf_utils_hw/types.hpp"
#include "xf_utils_hw/common.hpp"

namespace xf {
namespace common {
namespace utils_hw {

template <typename _TIn>
void streamDouble(hls::stream<_TIn>& istrm,
                  hls::stream<bool>& e_istrm,
                  hls::stream<_TIn>& ostrm,
                  hls::stream<bool>& e_ostrm);

} // utils_hw
} // common
} // xf

namespace xf {
namespace common {
namespace utils_hw {

template <typename _TIn>
void streamDouble(hls::stream<_TIn>& istrm,
                  hls::stream<bool>& e_istrm,
                  hls::stream<_TIn>& ostrm,
                  hls::stream<bool>& e_ostrm) {
    bool e = e_istrm.read();          // 前瞻：先读第一个 flag
    while (!e) {
#pragma HLS pipeline II = 1
        _TIn tmp;
        e = e_istrm.read();           // 读下一个 flag（看是否结束）
        tmp = istrm.read();           // 读数据
        ostrm.write(tmp * 2);         // 逐元素乘 2
        e_ostrm.write(false);         // 本拍尚未结束
    }
    e_ostrm.write(true);              // 输出 end flag
}

} // utils_hw
} // common
} // xf

#endif // XF_UTILS_HW_STREAM_DOUBLE_H
```

**需要观察的现象**：注意 `bool e = e_istrm.read()` 在循环外先读一次、循环内又读，这是「N 个数据读 N+1 个 flag」的前瞻式消费，与 `streamDup` 第 92/96 行完全一致——生产者必须配套写 N 个 `false` + 1 个 `true`。

**预期结果**：头件能被独立编译（include 守卫与命名空间正确），暂不产生硬件。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `types.hpp` 必须在 `ap_int.h` 之前把 `AP_INT_MAX_W` 设大？
**答案**：`ap_int.h` 在首次包含时根据 `AP_INT_MAX_W` 固化允许的最大位宽。若先引 `ap_int.h` 再设 `AP_INT_MAX_W`，宏不会生效，宽 `ap_int` 类型会编译失败。`types.hpp` 第 79 行先 `#undef` 再 `#define`，第 81 行才引 `ap_int.h`，正是为了规避这一点。

**练习 2**：`streamDouble` 为什么不像 `streamDup` 那样需要 `unroll`？
**答案**：`streamDup` 要把一个元素同时写到 `_NStrm` 路输出，循环 `for(i=0;i<_NStrm;i++)` 必须 `unroll` 才能在一拍内并发写 N 个端口。`streamDouble` 只有一路输出，循环体内没有需要并发的多路写，故无需 `unroll`，只保留 `pipeline II=1` 即可。

---

### 4.2 DUT 封装与 end flag

#### 4.2.1 概念说明

模板内核本身不能被 HLS 直接综合，原因有二：模板参数未钉死、C++ name mangling 让函数名带乱码。**DUT（Design Under Test）** 就是解决这两点的「实例化壳」——一个 `extern "C"` 顶层函数，做三件事：

1. **钉死模板参数**：把 `_TIn`、`_NStrm` 等用具体的 `typedef` 与宏常量绑定。
2. **压平签名**：把模板里的引用/数组参数转成固定形状的形参。
3. **关闭 name mangling**：`extern "C"` 让符号名就是函数名本身，便于主机侧 XRT 按名查找，也便于 `description.json` 的 `topfunction` 引用。

DUT 的名字必须与 `description.json` 的 `topfunction`、`hls_config.tmpl` 的 `syn.top` 三处一致。

#### 4.2.2 核心流程

DUT 封装的标准动作：

1. 在文件顶部用 `#define`/`typedef` 把类型与路数钉死（如 `typedef uint32_t TYPE;` 与 `#define NUM_COPY 16`）。
2. 写 `extern "C" void dutX(...)`，形参用已钉死的类型。
3. 函数体只有一行：调用模板内核、把实参传进去。
4. end flag 的拓扑随数据拓扑走——`streamDup` 各路独立结束，所以 end 是数组 `e_ostrms[NUM_COPY]`；单进单出的 `streamDouble` 只需一条 end 流。

#### 4.2.3 源码精读

`stream_dup` 的 test.cpp 顶部先用宏钉死类型与路数：

[utils/L1/tests/stream_dup/test.cpp:26-31](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L26-L31) —— `typedef uint32_t TYPE;` 与 `LEN_STRM/NUM_ISTRM/NUM_DSTRM/NUM_COPY` 四个宏，把模板参数全部钉死为编译期常量。

接着是 DUT 本体——注意 `extern "C"`、形参数组维度与模板实参的对应：

[utils/L1/tests/stream_dup/test.cpp:33-38](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L33-L38) —— `dut0` 是 `streamDup<TYPE, NUM_COPY>` 的壳：`extern "C"` 关闭 mangling，形参 `ostrms[NUM_COPY]` 与模板的 `_NStrm=NUM_COPY` 对齐，函数体只调用一次模板内核。

注意 DUT 与 testbench 共用一个 test.cpp，靠 `__SYNTHESIS__` 宏切换身份：

[utils/L1/tests/stream_dup/test.cpp:50](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L50) —— `#ifndef __SYNTHESIS__` 之下的代码（testbench 与 main）只在仿真时编译；综合时该宏被定义，这部分被剔除，剩下的 `dut0/dut1` 就是纯硬件顶层。这就是「一份 test.cpp 同时充当 DUT 与 testbench」的关键。

#### 4.2.4 代码实践

**实践目标**：为 `streamDouble` 写一个 `extern "C"` DUT，名字叫 `dut`。

**操作步骤**：

1. 在新建的 `stream_double/test.cpp` 顶部加 `typedef uint32_t TYPE;`。
2. 写 `extern "C" void dut(...)`，单进单出各带一条 end flag。
3. 函数体调用 `streamDouble<TYPE>(...)`。

参考实现（**示例代码**）：

```cpp
#include "xf_utils_hw/stream_double.hpp"
#include "ap_int.h"
#include "hls_stream.h"

typedef uint32_t TYPE;

extern "C" void dut(hls::stream<TYPE>& istrm,
                    hls::stream<bool>& e_istrm,
                    hls::stream<TYPE>& ostrm,
                    hls::stream<bool>& e_ostrm) {
    xf::common::utils_hw::streamDouble<TYPE>(istrm, e_istrm, ostrm, e_ostrm);
}
```

**需要观察的现象**：对比 `dut0` 的 4 参数（多路数组）与本 DUT 的 4 参数（单路标量引用）。end 流的形状完全跟随数据流——单进单出对应单条 `e_ostrm`，与 `stream_dup` 的 `e_ostrms[NUM_COPY]` 数组形成对照。

**预期结果**：该 DUT 可被 HLS 识别为综合顶层，函数符号名就是 `dut`（无 mangling）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `extern "C"` 去掉，会发生什么？
**答案**：C++ 会做 name mangling，符号表里的函数名变成带参数编码的乱码（如 `_Z3dutRN3hls6streamIjEE…`）。`description.json` 的 `topfunction: "dut"` 与 `hls_config.tmpl` 的 `syn.top=dut` 会找不到对应符号，综合失败。`extern "C"` 强制使用 C 链接约定，符号名即 `dut`。

**练习 2**：DUT 的形参 `hls::stream<TYPE>& istrm` 用引用而非值，为什么综合时没问题？
**答案**：HLS 工具会把 `hls::stream` 类型的顶层形参综合成硬件 FIFO 接口（如 AXI Stream 或内部握手通道），引用只是 C++ 层的传参约定，不影响综合后的接口形态。引用避免拷贝构造，也符合全库「流只能引用、不能复制」的语义。

---

### 4.3 testbench 编写

#### 4.3.1 概念说明

DUT 只能在硬件里跑，验证它的功能要靠 **testbench**——一段只在仿真时编译（被 `__SYNTHESIS__` 排除）的 C++ 程序。它的职责是：构造输入、调用 DUT、比对输出、统计错误数 `nerr` 并打印 `PASS`/`FAIL`。

`utils` 的 testbench 有一个统一结构，分三段：

1. **generate test data**：生成输入数据与黄金参考（golden）。
2. **test module**：把数据喂进流（含 end flag），调用 DUT。
3. **check result**：逐元素读出，与 golden 比对，并校验 end flag 的数量与最终 `true`。

`main` 根据 `argv[1]` 选择跑哪个测试函数——csim 固定传 `"0"`（见 4.4 节）。

#### 4.3.2 核心流程

写一个 testbench 的标准动作：

1. 声明输入/输出流与对应的 end 流。
2. 循环写入 N 个数据，每个数据配一个 `e_istrm.write(false)`；循环外补一个 `e_istrm.write(true)`——共 N 个 false + 1 个 true，匹配 4.2 节的前瞻式消费。
3. 调用 DUT。
4. 用 `read_nb`（非阻塞读）逐个校验输出与 golden、统计 `nerr`。
5. 校验 end flag：前 N 个应为 `false`，第 N+1 个应为 `true`。
6. `main` 调用测试函数，按 `nerr` 打印 `PASS`/`FAIL`，并把 `nerr` 作为进程退出码。

#### 4.3.3 源码精读

看 `stream_dup` 的 testbench 如何喂数据——注意 false/true 的配比：

[utils/L1/tests/stream_dup/test.cpp:81-87](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L81-L87) —— 对每路输入，循环写 `LEN_STRM` 个数据 + 配套 `false`，循环外再写一个 `true`。这正是「N 个 false + 1 个 true」的前瞻式约定，与 `streamDup` 循环里多读一次 flag 对应。

再看结果校验——用 `read_nb` 非阻塞读，既取数据又检测「数据丢失」：

[utils/L1/tests/stream_dup/test.cpp:102-113](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L102-L113) —— 三重循环逐个 `read_nb`：取不到数据记 `data loss` 错误，取到但与 golden 不符记 `nerr++`。`read_nb` 返回 `bool` 表示是否取到，这是检测「DUT 输出数量不对」的关键。

最后是 end flag 的校验——前 N 个必须 `false`、最后一个必须 `true`：

[utils/L1/tests/stream_dup/test.cpp:128-145](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L128-L145) —— 校验复制数据的 end flag：数据段每个 flag 应为 `false`（第 135 行 `else if (e) nerr++`），末尾第 138 行多读一次应为 `true`（第 142 行 `else if (!e) nerr++`）。end flag 的数量与取值都被严格校验。

`main` 用 `argv[1]` 选测试，并按 `nerr` 打印结论：

[utils/L1/tests/stream_dup/test.cpp:261-283](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L261-L283) —— `main` 依 `argv[1][0]` 分派到 `test_dut0`/`test_dut1`，按 `nerr` 打印 `PASS`/`FAIL`，`return nerr` 把错误数作为退出码交给 CI 判定（0 即 PASS）。

#### 4.3.4 代码实践

**实践目标**：为 `streamDouble` 的 DUT 写一个 testbench，含喂入、调用、校验三段。

**操作步骤**：

1. 在 `stream_double/test.cpp` 的 DUT 之后、`#ifndef __SYNTHESIS__` 之内写 `int test_dut()`。
2. 喂 N 个 `i`（配套 false）+ 1 个 true。
3. 调用 `dut(...)`。
4. 校验输出 `out == i*2`、end flag 配比。

参考实现（**示例代码**）：

```cpp
#ifndef __SYNTHESIS__

#include <iostream>

int test_dut() {
    int nerr = 0;
    const int N = 16;

    hls::stream<TYPE> istrm, ostrm;
    hls::stream<bool> e_istrm, e_ostrm;

    // ===== generate & feed =====
    for (int i = 0; i < N; i++) {
        istrm.write(static_cast<TYPE>(i));
        e_istrm.write(false);
    }
    e_istrm.write(true);   // N 个 false + 1 个 true

    // ===== test module =====
    dut(istrm, e_istrm, ostrm, e_ostrm);

    // ===== check result =====
    for (int i = 0; i < N; i++) {
        TYPE out = ostrm.read();
        if (out != static_cast<TYPE>(i * 2)) {
            nerr++;
            std::cout << "mismatch at " << i << ": got " << out << "\n";
        }
        bool e = e_ostrm.read();
        if (e) nerr++;          // 数据段 flag 必须为 false
    }
    bool efinal = e_ostrm.read();
    if (!efinal) nerr++;        // 末尾 flag 必须为 true

    return nerr;
}

int main(int argc, const char* argv[]) {
    int nerr = test_dut();
    if (nerr) std::cout << "\nFAIL: nerror= " << nerr << " errors found.\n";
    else      std::cout << "\nPASS: no error found.\n";
    return nerr;
}

#endif
```

**需要观察的现象**：若你「忘记」写 `e_istrm.write(true)`，DUT 的 `while(!e)` 会因为永远读不到 `true` 而一直阻塞在读 flag 上（仿真挂起）。这就是 end flag 约定的强约束——喂入与消费必须严格配比。

**预期结果**：csim 下应打印 `PASS: no error found.`，进程退出码为 0。具体运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：校验时用 `read`（阻塞）还是 `read_nb`（非阻塞）更合适？为什么 `stream_dup` 选了 `read_nb`？
**答案**：`read_nb` 更合适。阻塞 `read` 在流空时会永远挂起，无法区分「DUT 还没产出」与「DUT 产出的数量不够」。`read_nb` 返回是否取到，取不到即可记 `data loss` 错误并继续，从而能在 DUT 输出数量错误时也给出明确判定而非死锁。

**练习 2**：为什么校验 end flag 时，数据段每个应为 `false`、末尾那个才应为 `true`？
**答案**：`streamDouble` 在循环内每拍写 `false`、退出循环后写一个 `true`。所以前 N 个 flag 必为 `false`（表示数据仍在流动），第 N+1 个必为 `true`（表示流结束）。校验这两个不变量能确认 DUT 没有提前或延后发出结束信号。

---

### 4.4 Makefile 与 description.json

#### 4.4.1 概念说明

光有 test.cpp 还跑不起来，需要三件配套把用例接入工具链与 CI：

- **`Makefile`**：驱动 `make run TARGET=csim`，把模板渲染成配置、再分派给 `v++ -c`（综合）或 `vitis-run`（仿真）。**它是完全通用的**——不写死任何用例名，靠 `CUR_DIR` 自动定位配置文件，所以新用例可逐字复制。
- **`hls_config.tmpl`**：HLS 配置模板，声明综合顶层 `syn.top`、源文件、`csim.argv` 等，用 `${VIVADO_FLOW}` 等占位符。
- **`description.json`**：用例的 CI 身份证，`flow`/`topfunction`/`platform_allowlist`/`testinfo.targets` 等字段决定它走哪条流水线、在哪些平台跑、CI 跑哪几档。

这三件加上 test.cpp，就是一个 L1 用例的完整交付物。

#### 4.4.2 核心流程

构建一个新用例的元数据流程：

1. **复制 Makefile**：从 `stream_dup` 逐字复制，无需改动（它从 `$(lastword $(MAKEFILE_LIST))` 自定位）。
2. **改 `hls_config.tmpl`**：把 `syn.top` 改成你的 DUT 名（如 `dut`），`csim.argv` 与 testbench 的 `argv` 对齐。
3. **写 `description.json`**：`topfunction` 与 `syn.top` 一致，`flow` 设为 `"hls"`，`testbench.argv.hls_csim` 与 `csim.argv` 一致，`testinfo.targets` 列出要跑的档位。
4. **跑 `make run TARGET=csim`**：Makefile 先渲染 `hls_config.cfg`，csim 档只跑 `vitis-run`（不综合），打印 PASS/FAIL。
5. **跑 `make run TARGET=csynth`**：csynth 档只跑 `v++ -c`，产出 `test.prj` 综合报告。

Makefile 内部如何分派，是理解整个流程的关键。

#### 4.4.3 源码精读

先看 Makefile 的「模板渲染」——用内嵌 Python 把 `${VAR}` 占位符替换成环境变量：

[utils/L1/tests/stream_dup/Makefile:160-166](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L160-L166) —— `define CONFIG_GEN_PY` 内联一段 Python：读 `$(CONFIG_TMPL)`、用 `string.Template(...).substitute(**dict(os.environ))` 把 `${XF_PROJ_ROOT}`、`${VIVADO_FLOW}` 等占位符替换为环境变量，写出 `$(CONFIG_FILE)`。这就是 u14-l1 讲的「模板渲染」机制。

再看 Makefile 的「TARGET 分派」——csim 跳过 `v++ -c`、csynth 跳过 `vitis-run`：

[utils/L1/tests/stream_dup/Makefile:178-187](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L178-L187) —— `all` 目标在「非 csim」时跑 `v++ -c --mode hls`（综合）；`run` 目标在「非 csynth」时跑 `vitis-run --mode hls --$(TARGET_REL)`（仿真）。两段 `ifneq` 的互补，使得 csim 只仿真不综合、csynth 只综合不仿真，其余三档（cosim/vivado_syn/vivado_impl）两者都跑。注意 `$(TARGET_REL)` 是小写化的目标名，`--$(TARGET_REL)` 展开成 `--csim`/`--csynth` 等。

注意 Makefile 的默认平台与 TARGET——它默认就指向 vck190、csim：

[utils/L1/tests/stream_dup/Makefile:50-56](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L50-L56) —— `PLATFORM ?= xilinx_vck190_base_202610_1`、`TARGET ?= csim`、`CONFIG_FILE`/`CONFIG_TMPL` 都取自 `$(CUR_DIR)`。这三个默认值意味着裸 `make run` 即可在 vck190 上跑 csim，且配置文件自动从本目录读取——这正是 Makefile 可逐字复用的根因。

接着看 `hls_config.tmpl`——注意 `syn.top` 与 `csim.argv`：

[utils/L1/tests/stream_dup/hls_config.tmpl:1-17](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/hls_config.tmpl#L1-L17) —— 第 7 行 `syn.top=dut0` 指明综合顶层；第 11 行 `csim.argv=0` 把 `"0"` 作为参数传给 testbench 的 `main`（对应 `argv[1][0]=='0'` 走 `test_dut0`）；第 17 行 `vivado.flow=${VIVADO_FLOW}` 是唯一随 TARGET 变化的占位符。

最后看 `description.json` 的关键字段——它们如何被 CI 识别：

[utils/L1/tests/stream_dup/description.json:4-14](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/description.json#L4-L14) —— 第 4 行 `flow: "hls"` 决定走 HLS 流水线；第 5-7 行 `platform_allowlist: ["vck190"]` 限定平台；第 14 行 `topfunction: "dut0"` 与 `syn.top` 必须一致——这是综合顶级的「单一真相」。

argv 与 CI 档位的对应：

[utils/L1/tests/stream_dup/description.json:27-30](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/description.json#L27-L30) —— `testbench.argv.hls_csim: "0"` 与 `hls_config.tmpl` 的 `csim.argv=0`、test.cpp 的 `argv[1][0]=='0'` 三处必须一致，否则会跑错测试函数。

[utils/L1/tests/stream_dup/description.json:57-64](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/description.json#L57-L64) —— `testinfo.targets` 列出五档大写 TARGET，`category: "canary"` 表示这是全量回归用例（u14-l1 讲过 canary/full 之分）。

#### 4.4.4 代码实践

**实践目标**：为新用例 `stream_double` 准备 Makefile、`hls_config.tmpl`、`description.json`，并跑通 csim。

**操作步骤**：

1. 把 `stream_dup/Makefile` 逐字复制到 `stream_double/`（无需任何改动）。
2. 复制并改写 `hls_config.tmpl`，只把 `syn.top=dut0` 改成 `syn.top=dut`（**示例代码**）：

   ```ini
   [hls]
   clock=2.5
   flow_target=vivado
   syn.file=test.cpp
   syn.file_cflags=test.cpp,-I${XF_PROJ_ROOT}/L1/include
   syn.top=dut
   tb.file=test.cpp
   tb.file_cflags=test.cpp,-I${XF_PROJ_ROOT}/L1/include
   csim.argv=0
   cosim.argv=0
   vivado.flow=${VIVADO_FLOW}
   vivado.rtl=verilog
   ```

3. 写 `description.json`，把 `topfunction` 改成 `dut`、`name`/`description` 改成 Stream Double（**示例代码**）：

   ```json
   {
       "name": "Xilinx Stream Double HLS Test",
       "description": "Xilinx Stream Double HLS Test",
       "flow": "hls",
       "platform_allowlist": ["vck190"],
       "platform_blocklist": [],
       "part_allowlist": [],
       "part_blocklist": [],
       "project": "test",
       "solution": "solution1",
       "clock": "2.5",
       "topfunction": "dut",
       "top": {
           "source": ["test.cpp"],
           "cflags": "-I${XF_PROJ_ROOT}/L1/include"
       },
       "testbench": {
           "source": ["test.cpp"],
           "cflags": "-I${XF_PROJ_ROOT}/L1/include",
           "ldflags": "",
           "argv": {"hls_csim": "0", "hls_cosim": "0"},
           "stdmath": false
       },
       "testinfo": {
           "disable": false,
           "jobs": [
               {"index": 0, "dependency": [], "env": "", "cmd": "",
                "max_memory_MB": {"vivado_syn":16384,"hls_csim":10240,"hls_cosim":16384,"vivado_impl":16384,"hls_csynth":10240},
                "max_time_min": {"vivado_syn":420,"hls_csim":60,"hls_cosim":420,"vivado_impl":420,"hls_csynth":60}}
           ],
           "targets": ["hls_csim","hls_csynth","hls_cosim","vivado_syn","vivado_impl"],
           "category": "canary"
       },
       "gui": true
   }
   ```

4. 在 `stream_double/` 下执行：

   ```bash
   make run TARGET=csim
   ```

**需要观察的现象**：Makefile 会先用 Python 渲染出 `hls_config.cfg`，再调 `vitis-run --mode hls --csim`。csim 档不会综合，所以速度快（约分钟级）。终端应打印 `PASS: no error found.`。

**预期结果**：csim 通过、退出码为 0。`make clean` 可清除 `hls_config.cfg`、`*_hls.log`、`hls/` 工作目录。本机是否装有 Vitis 工具链决定能否实际运行，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么新用例的 Makefile 可以逐字复制而不需改动？
**答案**：因为 Makefile 不硬编码任何用例名。它用 `$(lastword $(MAKEFILE_LIST))` 取自身路径推出 `CUR_DIR`，再用 `$(CUR_DIR)/hls_config.tmpl` 与 `$(CUR_DIR)/hls_config.cfg` 定位配置；`XF_PROJ_ROOT` 通过剥离路径中的 `/L1/*` 后缀自动得到。所有用例相关信息（顶层名、argv）都集中在 `hls_config.tmpl` 与 `description.json` 里，Makefile 本身与用例无关。

**练习 2**：如果 `description.json` 的 `topfunction` 写成 `dut`，而 `hls_config.tmpl` 的 `syn.top` 仍是 `dut0`，会怎样？
**答案**：CI 按 `description.json` 的 `topfunction` 期望综合 `dut`，但实际渲染出的 `hls_config.cfg` 里 `syn.top=dut0` 指向了不存在的函数（或错误的函数），综合阶段会因找不到顶层而失败。三处（`topfunction`、`syn.top`、test.cpp 里的 `extern "C"` 函数名）必须一致，这是本讲反复强调的「单一真相」。

**练习 3**：csim 档为什么既不综合、也不需要平台？
**答案**：csim 是纯软件功能仿真，把 test.cpp 当普通 C++ 程序编译运行（`__SYNTHESIS__` 未定义，testbench 与 main 生效），只验证算法逻辑正确性，不产生任何 RTL。因此 Makefile 用 `ifneq ($(TARGET_REL), csim)` 跳过 `v++ -c`，且不真正下载比特流——平台 `xilinx_vck190_base_202610_1` 在 csim 下仅用于反查 `XPART`，对功能结果无影响。

## 5. 综合实践

把 4.1～4.4 的碎片缝成一个完整可运行用例。任务：**实现 `stream_double`——把输入流每个元素乘 2 输出——的完整 L1 用例，包含 hpp/test.cpp/Makefile/hls_config.tmpl/description.json，并跑通 csim，再跑 csynth 读懂报告**。

完整步骤：

1. **建目录**：`utils/L1/tests/stream_double/`（用例侧）与内核头 `utils/L1/include/xf_utils_hw/stream_double.hpp`（内核侧）。把 4.1 的头件、4.2 的 DUT、4.3 的 testbench 拼进同一个 `test.cpp`（DUT 在前、`#ifndef __SYNTHESIS__` 包裹 testbench 与 main 在后）。
2. **配构建**：按 4.4 复制 Makefile、写 `hls_config.tmpl`（`syn.top=dut`）与 `description.json`（`topfunction=dut`）。
3. **跑 csim**：`make run TARGET=csim`，确认打印 `PASS: no error found.`。
4. **跑 csynth**：`make run TARGET=csynth`，打开 `hls/test/solution1/`（或 `test.prj`）下的综合报告。
5. **读报告**：找到以 top 函数命名的 `dut_csynth.rpt`，记录三项：
   - **II**（启动间隔）：因 `#pragma HLS pipeline II=1`，预期 II=1；
   - **Latency**：约等于数据长度 N + 流水填充拍数；
   - **资源**：`stream_double` 只做一次乘 2，预期 DSP≈1（乘法器）、BRAM 用于流 FIFO、LUT/FF 少量。

报告字段解读（承接 u2-l3）：

- 吞吐 \( \text{Throughput} = \dfrac{1}{\text{II}} \times f_{\text{clk}} \)。II=1 时每个时钟周期处理一个元素，吞吐即时钟频率本身。
- Latency 是处理 N 个元素的总周期数，约 \( N + \text{pipeline depth} \)。
- 若 DSP 不为 1 而是 0，说明综合器把 `*2` 优化成了移位（`<<1`）——这也是合理结果，可在报告的「乘法器」一栏确认。

**预期结果**：csim PASS；csynth 报告 II=1、Latency 与数据长度同阶、DSP 为 0 或 1。综合报告的具体数值**待本地验证**。

**延伸思考**：如果把 `stream_double` 改成乘以一个运行时变量（而非字面量 2），DSP 仍会是 0 吗？为什么？（提示：字面量 `*2` 会被强度折减为移位，运行时变量乘法则必须分配 DSP。）

## 6. 本讲小结

- 一个 L1 流式内核的完整交付物是 **5 个文件**：模板头件 `.hpp`（内核侧）+ `test.cpp`、`Makefile`、`hls_config.tmpl`、`description.json`（用例侧）。
- **模板头件**遵循 include 顺序（`types.hpp` 先撑大 `AP_INT_MAX_W` 再引 `ap_int.h`）、命名空间、前向声明 + 实现两段式，主循环靠 `pipeline II=1` 与必要的 `unroll`，参数约束用 `XF_UTILS_HW_STATIC_ASSERT` 编译期拦截。
- **DUT** 是模板内核的 `extern "C"` 实例化壳，钉死模板参数、压平签名、关闭 name mangling，其名字必须与 `topfunction`/`syn.top` 三处一致；与 testbench 共用 test.cpp，靠 `__SYNTHESIS__` 宏切换身份。
- **end flag** 采用前瞻式消费（N 个数据读 N+1 个 flag），testbench 必须配套喂 N 个 `false` + 1 个 `true`，并校验输出的 flag 数量与最终 `true`。
- **Makefile 完全通用**，靠 `CUR_DIR` 自定位，可逐字复制；用例相关元数据全在 `hls_config.tmpl`（`syn.top`/`csim.argv`）与 `description.json`（`flow`/`topfunction`/`testinfo.targets`）里。
- **TARGET 分派**由 Makefile 的两段互补 `ifneq` 完成：csim 只仿真（`vitis-run`）、csynth 只综合（`v++ -c`）、其余三档两者都跑；csynth 报告的 II/Latency/资源三项是验收内核质量的核心指标。

## 7. 下一步学习建议

- **向 AIE 路线迁移**：本讲的 `stream_double` 是 PL（HLS）内核。若你想写 AIE 内核，下一步读 u6-l2（AIE 内核三件式：kernel + traits + utils）与 u13-l1（ADF 图、window/stream、PL↔AIE 边界），把「模板头件 + DUT」范式对应到「kernel.hpp + graph 包装器」。
- **加入数据搬运**：本讲的内核只有流接口，要让它真正上板还需 mm2s/s2mm 把 DDR 数据喂进流——读 u5-l2（数据搬运器与 DDR↔AIE 桥接）与 u5-l1（v++ L2 构建流程：XO→xsa→xclbin）。
- **提交到 CI**：想让你的新用例被仓库 CI 自动跑，确保 `description.json` 的 `category`（canary/full）与 `testinfo.targets` 设置正确，再读 u14-l1 的 Jenkinsfile「薄入口 + 厚共享库」机制，理解你的用例是如何被流水线发现的。
- **性能调优**：当 csynth 报告 II>1 或资源超限时，回到 u3-l2（pipeline/unroll/dataflow）与 u12-l1（dataflow、SSR、datawidth 与 II 调优）寻找对策。
