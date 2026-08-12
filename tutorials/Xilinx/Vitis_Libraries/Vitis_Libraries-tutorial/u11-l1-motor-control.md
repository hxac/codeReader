# 电机控制：FOC / SVPWM / PWM_GEN / QEI

## 1. 本讲目标

本讲聚焦 Vitis 加速库家族中**最垂直**的一个库——`motor_control`（电机控制）。它不像 `dsp`/`solver` 那样提供通用数值原语，而是面向**永磁同步电机（PMSM）驱动**这一具体应用，提供 4 个算法级 L1 API：

- **FOC**：磁场定向控制，把三相电流变换到旋转坐标系做闭环；
- **SVPWM_DUTY**：空间矢量脉宽调制的**前端**，把电压命令换算成占空比；
- **PWM_GEN**：空间矢量脉宽调制的**后端**，把占空比变成驱动开关管的门极脉冲；
- **QEI**：正交编码器接口，从编码器脉冲还原出转速和转子角度。

学完本讲，你应当能够：

1. 说出这 4 个 API 各自的职责，并能讲清 **SVPWM_DUTY（前端）与 PWM_GEN（后端）如何分工**完成一次空间矢量调制；
2. 看懂这些内核的**三类接口约定**：AXI Stream（数据流）、AXI-Lite（寄存器配置）、AXI-Lite 状态回读（`ap_none`），理解为什么这种"可配置 + 可观测"的接口是为 **IPI（IP integrator）图形化集成**而设计的；
3. 理解 `models_fp` 这一**新增的 FP32 浮点模型分支**与默认 `ap_fixed` 定点实现的差异，以及它如何服务于精度分析。

本讲承接 u1-l3（L1/L2/L3 与 PL/AIE 范式）与 u3-l1（`hls::stream`、`ap_int` 与 DUT 封装约定）建立的心智模型。motor_control 是一个**纯 PL（HLS）路线**的库，没有 AIE 实现，因此你不会在这里看到 ADF 图；但你会看到 HLS 流式内核、`#pragma HLS` 指令与 AXI 接口约定在本讲里被用到极致。

---

## 2. 前置知识

在进入源码前，先用最通俗的方式建立几个电机控制的直觉概念。即便你从未碰过电机，只要理解下面几个比喻就能跟上。

### 2.1 为什么要"控制"电机

一个三相永磁同步电机有三根相线（A/B/C），通上随时间变化的三相正弦电流就能让转子转动。要让转子**精确地**停在某个角度、以某个速度转、或输出某个力矩，需要在每一个极短的控制周期（典型 100µs）里，根据**当前测得的电流和转子位置**，算出"下一刻三相绕组该施加多大电压"。这就是 FOC 要做的事。

### 2.2 三个坐标系变换：Clarke / Park / 反变换

直接控制三相电流 `Ia/Ib/Ic` 很别扭，因为它们互相耦合。FOC 的核心技巧是两次坐标变换，把问题简化：

- **Clarke 变换**：把三相 `abc` 电流变到两相静止坐标系 `αβ`（3 个量变 2 个量，外加一个零序量）。
- **Park 变换**：把静止 `αβ` 变到**跟随转子旋转**的坐标系 `dq`。在 `dq` 坐标系里，电流的两个分量 `Id`（磁通）和 `Iq`（力矩）**解耦**了——`Iq` 直接控制力矩、`Id` 直接控制磁通，可以分别用 PID 闭环。
- **反变换**：算完 `dq` 上的控制电压后，再反 Park、反 Clarke，变回三相电压命令 `Va_cmd/Vb_cmd/Vc_cmd`，送去做调制。

旋转需要知道**转子当前角度**，这个角度由编码器（QEI）给出。

### 2.3 SVPWM：把电压命令变成开关动作

逆变器有 6 个开关管（每相上下各一个），只能输出离散的电压组合。SVPWM（空间矢量脉宽调制）的目标是：给定三相电压命令 `Va/Vb/Vc`，找到一组占空比，让逆变器**平均输出**的电压尽可能逼近命令值。本库把它拆成两段：

- **前端（SVPWM_DUTY）**：算出三相的**占空比**（一个 0~1 的小数）。
- **后端（PWM_GEN）**：根据占空比，在一个 PWM 周期内生成 6 个开关管的高/低电平门控信号，并加入**死区（dead cycles）**防止上下管直通短路。

### 2.4 QEI：从脉冲读速度和位置

正交编码器输出三路方波 A/B/I：A、B 相差 90°，方向靠"A 先还是 B 先"判断；I 每转一圈一个脉冲，用于归零。QEI 模块数脉冲得到**位置**，测两次脉冲之间的时钟周期数得到**速度**。

### 2.5 定点数 ap_fixed 与"位宽即精度"

真实硬件里用 `float` 太贵（占 DSP、慢），所以 motor_control 默认用 `ap_fixed<W, I>`（共 W 位、其中 I 位整数）定点数。定点数会引入量化误差，位宽越大越准但越占资源——这正是 `models_fp` 分支要做精度对比的原因。u3-l1 已讲过 `ap_int`，`ap_fixed` 是它的"带小数点"版本。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [motor_control/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/README.md) | 库定位、4 个 API 概述、2026.1 关于源码回退与 FP32 分支的说明 |
| [motor_control/L1/include/hw/common.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/common.hpp) | 全局宏（时钟、CPR、电机参数）、共享定点类型（`t_glb_foc2pwm`/`t_glb_q15q16`）、`RangeDef`/`CheckRange` 范围校验 |
| [motor_control/L1/include/hw/foc.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/foc.hpp) | FOC 顶层 `hls_foc_strm_ap_fixed`、`FOC_Mode` 枚举、Q15.16 正余弦查找表、`foc_core_ap_fixed` 主流程 |
| [motor_control/L1/include/hw/svpwm.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/svpwm.hpp) | SVPWM 前端 `hls_svpwm_duty_axi` + 后端 `hls_pwm_gen_axi`，含占空比核心 `calculate_ratios_core`、波形生成 `PWM_gen_wave` |
| [motor_control/L1/include/hw/qei.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/qei.hpp) | QEI 顶层 `hls_qei_axi`、滤波 `filterIn`、捕边 `catchingEdge`、计数 `calcCounter` |
| [motor_control/L1/tests/IP_SVPWM/src/ip_svpwm.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/tests/IP_SVPWM/src/ip_svpwm.cpp) | SVPWM 前后端的 **DUT 封装**（`extern` 顶层、AXI 接口绑定） |
| [motor_control/L1/tests/IP_SVPWM/src/test_svpwm.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/tests/IP_SVPWM/src/test_svpwm.cpp) | 测试台，其 `SVPWM_wrapper` 用 DATAFLOW 把前端→后端串起来 |
| [motor_control/L1/include/models_fp/svpwm_fp32.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/models_fp/svpwm_fp32.hpp) | FP32 浮点 SVPWM 参考模型，用于精度对比 |
| [motor_control/L1/include/models_fp/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/models_fp/README.md) | 解释 `_fp32`（可综合）与无后缀（模板黄金参考）两种模型 |

库的 `library.json` 只声明一个 include 路径 `LIB_DIR/L1/include`（内核侧与主机侧共用），说明它是一个**纯头件库**，所有实现都在 `L1/include/hw` 下的模板函数里，由各 `IP_*` 测试目录实例化与综合。

---

## 4. 核心概念与源码讲解

### 4.1 FOC：磁场定向控制

#### 4.1.1 概念说明

FOC 是整个电机驱动的"大脑"。它的工作流可以用一句话概括：

> 测三相电流 → Clarke → Park → 在 dq 坐标系用 PID 算控制电压 → 反 Park → 反 Clarke → 输出三相电压命令。

motor_control 的 FOC 还额外支持**弱磁（field weakening）**控制——当电机高速运转、反电动势逼近直流母线电压时，主动压低 `Id` 以扩展调速范围；以及**多种工作模式**（纯力矩、纯磁通、手动给定等），由一个 `FOC_Mode` 寄存器切换。

FOC 是一个**流式内核**：它在一个近乎无限长的循环里，每个控制周期消费一组电流/转速/角度输入，产出一组电压命令输出。输入输出走 AXI Stream，而所有 PID 参数、模式、设定点都走 AXI-Lite 寄存器，运行时由处理器改写——这正是"在硬件里跑一个实时控制律"的典型形态。

#### 4.1.2 核心流程

```
        AXI Stream 输入                    AXI Stream 输出
  Ia,Ic,Ic ────────┐               ┌──────── Va_cmd,Vb_cmd,Vc_cmd
  RPM&Theta_m ─────┤               │
                   ▼               │
            ┌──────────────────────────────────┐
            │            FOC 内核               │   ◄── AXI-Lite 参数：
            │  1. 角度折算 (Theta)              │       control_mode, ppr,
            │  2. Clarke_Direct_3p → Iα,Iβ      │       flux/torque/speed 的
            │  3. 查表 cos/sin(Theta)           │       sp/kp/ki/kd, vd, vq,
            │  4. Park_Direct → Id,Iq           │       fw_kp/fw_ki (弱磁) ...
            │  5. Speed PID / Flux PID /        │   ──► AXI-Lite 状态：
            │     Torque PID (各一路 PID)       │       id/iq_stts, 各 PID 的
            │  6. 解耦 Decoupling               │       acc/err/out_stts,
            │  7. Field_Weakening 弱磁          │       speed/angle_stts ...
            │  8. Control_foc 按模式选 Vd,Vq     │
            │  9. Park_Inverse → Vα,Vβ          │
            │ 10. Clarke_Inverse_2p → Va,Vb,Vc  │
            │ 11. Clip 限幅                      │
            └──────────────────────────────────┘
```

控制律在数学上是连续的，但 FOC 内核每个周期用 **Q15.16 定点格式**（即 `t_glb_q15q16 = ap_fixed<32,16>`）承载 PID 设定点、反馈与中间量；而输入输出电压/电流用 `ap_fixed<24,8>`（`t_glb_foc2pwm`），见 `common.hpp`。

正余弦函数不做实时计算，而是查一张覆盖一整圈的表 `sin_table[1000]`/`cos_table[1000]`，表长等于 `COMM_MACRO_CPR`（编码器每转步数 1000）。这是 HLS 里典型的"用 BRAM 换 DSP"做法。

#### 4.1.3 源码精读

**FOC_Mode 枚举**——9 种工作模式，是 FOC 内核最外层的"状态选择器"：

[FOC_Mode 枚举 — foc.hpp:340-353](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/foc.hpp#L340-L353)

```cpp
enum FOC_Mode {
    MOD_STOPPED = 0,
    MOD_SPEED_WITH_TORQUE,      // 速度外环 + 力矩/磁通内环
    MOD_TORQUE_WITHOUT_SPEED,   // 关闭速度环，直接给力矩设定点
    MOD_FLUX,
    MOD_MANUAL_TORQUE_FLUX_FIXED_SPEED,  // 手动 Vd/Vq + 内部生成角度
    MOD_MANUAL_TORQUE_FLUX,
    MOD_MANUAL_TORQUE,
    MOD_MANUAL_FLUX,
    MOD_MANUAL_TORQUE_FLUX_FIXED_ANGLE,  // 手动 Vd/Vq + 外部给角度
    MOD_TOTAL_NUM
};
```

模式由寄存器 `control_mode_args` 在运行时切换。模式如何改变行为？看 `Control_foc_ap_fixed` 里的 `switch`：

[Control_foc_ap_fixed 的 switch 分发 — foc.hpp:384-447](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/foc.hpp#L384-L447)

它根据模式决定 `Vd/Vq` 是来自 PID 输出、还是寄存器手动给定，以及角度用实测、内部生成还是外部给定。例如 `MOD_SPEED_WITH_TORQUE` 把 `Vd=Flux_out`、`Vq=Torque_out`（都来自 PID），而 `MOD_MANUAL_TORQUE_FLUX` 把它们改成寄存器值 `args_vd/args_vq`。

**FOC 流式顶层**——这是真正被综合的模板函数。注意 `II = 5` 的流水、输入流的非阻塞读取、以及把 RPM 和角度打包在同一个 32 位流里：

[hls_foc_strm_ap_fixed 顶层 — foc.hpp:1011-1119](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/foc.hpp#L1011-L1119)

关键片段（节选）：

```cpp
LOOP_FOC_STRM:
    for (long i = 0; i < trip_cnt; i++) {
#pragma HLS pipeline II = 5
        ...
        short RPM_in   = (FOC_RPM_THETA_m_in & 0x0000FFFF);        // 低16位=转速
        short Angle_in = (FOC_RPM_THETA_m_in & 0xFFFF0000) >> 16;  // 高16位=角度

        details::foc_core_ap_fixed<VALUE_CPR, T_IO, MAX_IO, W, I>(
            Ia_in, Ib_in, Ic_in, RPM_in, Angle_in, Va_out, Vb_out, Vc_out, ...);
        ...
    }
```

两点值得注意：

1. **`II = 5`**：FOC 的控制律很重（三路 PID + 解耦 + 弱磁 + 两次坐标变换），单周期做不完，所以每 5 个时钟周期处理一个采样。100MHz 时钟下 II=5 意味着每 50ns 一个控制律更新，仍远快于 100µs 的典型控制周期。
2. **RPM 与角度共用一条流**：`FOC_RPM_THETA_m` 把转速放低 16 位、角度放高 16 位，省一条流。

`foc_core_ap_fixed` 是真正的控制律实现（约 500 行），依次调用 `Clarke_Direct_3p_ap_fixed` → 查表 → `Park_Direct_ap_fixed` → 三路 `PID_Control_ap_fixed` → `Decoupling` → `Field_Weakening_T` → `Control_foc_ap_fixed` → `Park_Inverse_ap_fixed` → `Clarke_Inverse_2p_ap_fixed` → `Clip_AP` 限幅。这部分链路在 [foc_core_ap_fixed — foc.hpp:452-943](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/foc.hpp#L452-L943)，你阅读时按 `RANGETRACER(...)` 标记的顺序走即可——每一步都被命名了。

#### 4.1.4 代码实践：源码阅读型

**实践目标**：在不实际综合的情况下，靠读源码理清 FOC 的数据通路与参数接口。

**操作步骤**：

1. 打开 `motor_control/L1/include/hw/foc.hpp`，定位 `hls_foc_strm_ap_fixed`（L1011）。
2. 数一数它的 `volatile int&` 形参：哪些以 `_args` 结尾（输入参数，主机写）、哪些以 `_stts` 结尾（状态回读，主机读）。
3. 进入 `foc_core_ap_fixed`（L452），顺着 `RANGETRACER("FOC.CLARK...")` → `"FOC.PARK..."` → `"FOC.PID.Speed..."` → `"FOC.Flux_pid..."` → `"FOC.Torque_pid..."` → `"FOC.Decoupling..."` → `"FOC.Field_W..."` → `"FOC.InversPark..."` → `"FOC.InversClarke..."` 的标记读一遍。
4. 看 `Control_foc_ap_fixed`（L367）的 `switch`，确认 `MOD_STOPPED` 时 `Vd=Vq=0`（停机），`MOD_SPEED_WITH_TORQUE` 时 `Vd/Vq` 来自 PID。

**需要观察的现象**：FOC 的参数接口数量极多（PID 的 sp/kp/ki/kd 就有 4 路 × 4 个环路 = 16 个），几乎全部是 `volatile int&`（AXI-Lite），这正是它"可运行时配置"的体现。

**预期结果**：你应能画出"电流进 → Clarke/Park → 三路 PID → 解耦/弱磁 → 反变换 → 电压出"的完整链路，并指出 `vd_args/vq_args` 仅在 MANUAL 模式下才被使用。

**待本地验证**：实际 II、资源占用需 `make run TARGET=csynth` 后查看报告。

#### 4.1.5 小练习与答案

**练习 1**：FOC 为什么需要 `II=5` 而不是 `II=1`？
**参考答案**：FOC 单次迭代包含三路 PID、解耦、弱磁和两次坐标变换，运算密度高，组合逻辑深；HLS 无法在一个时钟周期内完成，故放宽到 II=5 以满足时序。控制周期（~100µs）远大于 5×10ns=50ns，吞吐仍绰绰有余。

**练习 2**：`sin_table`/`cos_table` 为什么放在 `foc.hpp` 顶部、长度等于 `COMM_MACRO_CPR`？
**参考答案**：查表覆盖一整圈（360°），用编码器步数 `COMM_MACRO_CPR=1000` 作为索引长度，于是角度可直接作为下标，`cos_table[Theta]` 即该角度的 Q15 余弦值；用 BRAM 存表（`BIND_STORAGE ... impl=BRAM`）替代实时 `cos/sin` 计算，省 DSP。

---

### 4.2 SVPWM_DUTY / PWM_GEN：前端 + 后端

#### 4.2.1 概念说明

FOC 输出的是三相电压命令 `Va_cmd/Vb_cmd/Vc_cmd`（一个有符号定点数，量纲是伏特）。但逆变器不能直接输出任意电压，它只有 6 个开关管，每个开关要么全开要么全关。SVPWM 的任务是：**用开关管的高速通断，让一个 PWM 周期内的平均电压逼近命令值**。

motor_control 把这件事拆成两个独立内核，**这是本讲最关键的设计**：

| 内核 | 角色 | 输入 | 输出 | 频率 |
|---|---|---|---|---|
| `hls_svpwm_duty`（SVPWM_DUTY） | **前端** | 三相电压命令 + 直流母线电压 | 三相占空比（0~1 的 16 位无符号） | 每次有新电压命令时算一次 |
| `hls_pwm_gen`（PWM_GEN） | **后端** | 三相占空比 | 6 路门控脉冲（h/l × A/B/C）+ 3 路同步信号 | 每个 PWM 时钟周期都跑 |

为什么要拆？因为前端是**事件驱动**（FOC 算完一次才算一次占空比），后端是**时间驱动**（必须严格按 PWM 频率生成波形）。两者速率不同，用一个流（占空比）解耦，前端慢、后端快，后端在没有新占空比时持续用上一个值生成波形。这恰好是 u3-l2 讲过的 **DATAFLOW 任务级流水**的典型应用场景。

#### 4.2.2 核心流程

**前端 SVPWM_DUTY 的数学（鞍形波 / min-max 调制）**：

给定三相电压命令 \(V_a, V_b, V_c\) 和直流母线参考 \(V_{ref}\)：

\[
V_{off} = \frac{\min(V_a,V_b,V_c) + \max(V_a,V_b,V_c)}{2}
\]

\[
V_{x,\text{saddle}} = V_x - V_{off}, \quad x \in \{a,b,c\}
\]

\[
\text{duty}_x = \frac{V_{x,\text{saddle}} + V_{ref}}{2 V_{ref}}
\]

这就是经典的"零序注入（min-max）"法：先减去零序分量 `Voff` 把正弦波压成鞍形，再除以 2 倍母线电压换算成占空比。它能让调制比线性区达到 1.1547（比正弦调制高 15%）。

**后端 PWM_GEN 的波形生成**：在一个 PWM 周期 `pwm_cycle = 时钟频率 / PWM 频率` 个时钟里，按占空比 `len = duty × pwm_cycle` 在周期中央产生一个高电平窗口，并对上桥 `h` 和下桥 `l` 各插入死区 `dead_cycles`，确保 `h` 和 `l` 永不同时导通。

#### 4.2.3 源码精读

**前端顶层 `hls_svpwm_duty_axi`**——用 DATAFLOW 把"采样"和"算占空比"两段并发：

[hls_svpwm_duty_axi — svpwm.hpp:651-676](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/svpwm.hpp#L651-L676)

```cpp
template <class T_FOC_COM, class T_RATIO_16b>
void hls_svpwm_duty_axi(...) {
    hls::stream<details::pwmStrmIO<T_FOC_COM> > strm_pwm_io_bundle;
#pragma HLS STREAM depth = 2 variable = strm_pwm_io_bundle
#pragma HLS DATAFLOW
    details::sampler_duty<T_FOC_COM>(pwm_args_cnt_trip, pwm_args_sample_ii, ...strm_pwm_io_bundle);
    details::calculate_ratios<T_FOC_COM, T_RATIO_16b>(strm_pwm_io_bundle, strm_duty_ratio_a, ...);
}
```

`sampler_duty` 按 `pwm_args_sample_ii` 间隔从 FOC 流里抽样电压命令；`calculate_ratios` 调下面的核心算占空比。

**占空比核心 `calculate_ratios_core`**——上面三行公式的直接翻译：

[calculate_ratios_core — svpwm.hpp:141-167](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/svpwm.hpp#L141-L167)

注意两步：`GetVoff` 求零序（鞍形化的第一步）：

[GetVoff — svpwm.hpp:114-123](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/svpwm.hpp#L114-L123)

```cpp
template <class T>
T GetVoff(T V[3]) {
    T Vmin, Vmax, Voff;
    Vmin = (V[0] < V[1]) ? V[0] : V[1];
    Vmin = (V[2] < Vmin) ? V[2] : Vmin;
    Vmax = (V[0] > V[1]) ? V[0] : V[1];
    Vmax = (V[2] > Vmax) ? V[2] : Vmax;
    Voff = (Vmin + Vmax) >> 1;   // 即 /2
    return Voff;
}
```

`GetRatio` 把鞍形电压夹紧到 `[-Vref, Vref]` 后换算成 0~1 占空比：

[GetRatio — svpwm.hpp:125-139](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/svpwm.hpp#L125-L139)

```cpp
ratio = ((Vp_saddle >> 1) / MAXVAL);   // (V_saddle + Vref)/2 / Vref
if (ratio >= 1) ratio = 0.99999;       // 防止满占空比（开关管需切换时间）
```

这里有个值得注意的细节：`MODE_PWM_DC_SRC` 选择母线电压参考的来源——是用 ADC 实测的（`DC_SRC_ADC`），还是用 AXI-Lite 寄存器静态配置的（`DC_SRC_REF`）：

[MODE_PWM_DC_SRC / MODE_PWM_PHASE_SHIFT 枚举 — svpwm.hpp:42-44](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/svpwm.hpp#L42-L44)

**后端顶层 `hls_pwm_gen_axi`**——直接调用 `PWM_gen_wave`：

[hls_pwm_gen_axi — svpwm.hpp:705-755](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/svpwm.hpp#L705-L755)

**单通道波形生成 `generate_output_chnl`**——这是"占空比 → 门控脉冲"的原子操作，对一个通道（如 A 相）算出上下桥 `h/l` 与同步 `sync`：

[generate_output_chnl — svpwm.hpp:243-296](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/svpwm.hpp#L243-L296)

核心逻辑（节选）：

```cpp
T_IN max_len = pwm_cycle - dead_cycles;        // 减死区后的最大脉宽
if (pwm_cnt2 == 0) len = tmp_len * pwm_cycle;  // 占空比→脉冲长度
if (len > max_len) len = max_len;
T_IN start = (pwm_cycle - len) >> 1;           // 脉冲居中
T_IN end   = start + len;
// 上桥 h：在 (start, end] 区间为 1
if (pwm_cnt2 <= start || pwm_cnt2 > end) h_pwm = 0; else h_pwm = 1;
// 下桥 l：与 h 反相，且各向外扩半个死区
if (pwm_cnt2 <= start - dead2 || pwm_cnt2 > end + dead2) l_pwm = 1; else l_pwm = 0;
// sync：在周期中央产生一个脉冲，用于触发 ADC 采样
if (pwm_cnt2 == (pwm_cycle >> 1)) pwm_sync = 1; else pwm_sync = 0;
```

关键设计点：

1. **脉冲居中**：`start = (pwm_cycle - len)/2`，让高电平窗口落在周期正中，对称性好、谐波小。
2. **死区**：`dead2 = (dead_cycles+1)/2`，下桥 `l` 比上桥 `h` 的边沿各推迟半个死区，确保 `h` 关断后 `l` 才导通，反之亦然——**避免上下管直通短路**。
3. **`SYNC` 信号**：在 PWM 周期中央发一个脉冲，专门用来**同步触发 ADC 采样电流**（在脉冲中点采样，电流最稳定）。
4. **移相模式**：`MODE_PWM_PHASE_SHIFT` 支持 0° 或 120° 移相（`shift[0/1/2]`），用于交错并联拓扑。

**前后端如何串起来**——这是理解整体的关键。在测试台 `SVPWM_wrapper` 里，前端 `hls_svpwm_duty` 的三个占空比输出流，就是后端 `hls_pwm_gen` 的输入流，二者在一个 DATAFLOW 区域里并发：

[SVPWM_wrapper 串联前端→后端 — test_svpwm.cpp:70-120](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/tests/IP_SVPWM/src/test_svpwm.cpp#L70-L120)

```cpp
#pragma HLS DATAFLOW
    hls_svpwm_duty(..., strm_duty_cycle_a, strm_duty_cycle_b, strm_duty_cycle_c, ...);  // 前端
    ...
    hls_pwm_gen(strm_duty_cycle_a, strm_duty_cycle_b, strm_duty_cycle_c,                // 后端
                strm_pwm_h_a, ..., strm_sync_c, ...);
```

`strm_duty_cycle_{a,b,c}` 三条流就是前后端的"粘合剂"：前端每产出一组占空比，后端就消费它生成后续若干个 PWM 周期的波形，直到下一组占空比到来。

#### 4.2.4 代码实践：修改参数观察波形

**实践目标**：理解前端三个关键参数（`pwm_freq`、`dead_cycles`、`phase_shift`）对生成波形的影响。

**操作步骤**：

1. 进入 `motor_control/L1/tests/IP_SVPWM`。
2. 运行 `make run TARGET=csim PLATFORM=xck26-sfvc784-2LV-c`（或用 `XPART`）。
3. 打开 `test_svpwm.cpp`，找到设置 `pwm_args_dead_cycles`、`pwm_args_phase_shift` 的测试激励处，把死区从默认值改大（如 20），重新跑 csim，观察输出 `strm_l_*` 与 `strm_h_*` 之间的"全 0 间隔"变宽。
4. 把 `pwm_args_phase_shift` 在 `SHIFT_ZERO(0)` 与 `SHIFT_120(1)` 之间切换，观察三相门控信号的相位关系变化。

**需要观察的现象**：死区增大时，每相 `h` 与 `l` 同时为 0 的间隔（即都不导通的"死区时间"）变长；移相模式下 B、C 相的波形相对 A 相整体平移。

**预期结果**：csim 通过（PASS），且能定性看到死区宽度与移相关系的变化。

**待本地验证**：csim 输出由测试台内部断言判定；若想看波形，可参考 `docs/src/images/tutorial_svpwm_duty_cosim_*.png` 这类文档已有的 cosim 截图。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GetRatio` 在 `ratio >= 1` 时强制设为 `0.99999` 而不是 `1`？
**参考答案**：占空比 1 意味着整个 PWM 周期内开关管始终导通、没有切换边沿，这会破坏死区逻辑并使 PWM 失去调制意义；强制略小于 1 保留切换时刻，确保死区与同步逻辑始终生效。

**练习 2**：前端 `hls_svpwm_duty` 与后端 `hls_pwm_gen` 为什么不合并成一个内核？
**参考答案**：两者速率不同——前端事件驱动（每收到一组电压命令算一次），后端时间驱动（按 PWM 频率持续生成波形）。拆分后用占空比流解耦，后端可在前端未更新时持续用上一组占空比工作；且各自可独立综合、独立优化 II 与资源，便于在 IPI 里当作两个独立 IP 摆放与连线。

---

### 4.3 QEI：正交编码器接口

#### 4.3.1 概念说明

FOC 要做 Park 变换必须知道**转子电角度**，PID 速度环需要**转速**。这两者由位置传感器给出，正交编码器（Quadrature Encoder）是最常见的一种。它输出三路数字脉冲：

- **A、B**：两路方波，相差 90°（"正交"）。谁先谁后决定**转向**；脉冲数累加得到**位置**。
- **I（Index）**：每转一圈一个脉冲，用于**绝对位置归零**。

QEI 内核是一个**纯数字信号处理**内核：它从三路可能有毛刺的原始脉冲流，经"滤波 → 捕边 → 计数"三级，最终还原出打包好的转速+角度（`RPM_THETA_m`，与 FOC 的输入格式对接）以及方向和错误状态。

#### 4.3.2 核心流程

```
  A,B,I 原始脉冲流
        │
        ▼
   filterIn ──── 数字噪声滤波：连续采样 16 次一致才认 → 打包成 4 位 f_ABI
        │
        ▼
  catchingEdge ─ 检测上升/下降沿 + 记录时间戳 timeStep → QEI_EdgeInfo
        │
        ▼
   calcCounter ─ 据边沿判方向、累加 4× 计数器得角度、
                  用两次边沿间隔 timeStep ÷ 时钟频率 ÷ CPR 得转速
        │
        ▼
  RPM_THETA_m（低16=有符号RPM, 高16=角度）+ dir + err
```

转速计算公式（4× 解码模式，每个 A/B 边沿都计数）：

\[
\text{RPM} = \frac{f_{\text{clk}} \times 60 / 4}{\text{ii\_cycles} \times \text{CPR}}
\]

其中 `ii_cycles` 是两次有效边沿之间的时钟周期数，CPR 是每转计数（编码器分辨率）。

#### 4.3.3 源码精读

**QEI 顶层 `hls_qei_axi`**——典型的三段 DATAFLOW：

[hls_qei_axi — qei.hpp:538-578](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/qei.hpp#L538-L578)

```cpp
#pragma HLS DATAFLOW
    details::filterIn<T_bin>(...);     // 滤波
    details::catchingEdge<T_bin>(...); // 捕边
    details::calcCounter<T_bin, T_err>(...); // 计数 + 算速度
```

**数字滤波 `filterIn`**——经典的"连续 N 次一致才采信"去抖：每个输入通道有个计数器，输入变化时清零，计数到 `max_filtercount=16` 才把滤波输出更新为当前输入。这能滤掉短于 16 个时钟的毛刺：

[filterIn — qei.hpp:74-128](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/qei.hpp#L74-L128)

```cpp
const ap_uint<5> max_filtercount = 16;
...
if (pre_a ^ ini_A) filter_a = 0;       // 输入一变，计数器清零
...
if (filter_a < max_filtercount) filter_a++;
if (filter_a == max_filtercount) f_a = ini_A;  // 稳定 16 拍才采信
```

**捕边 `catchingEdge`**——把滤波后的 ABI 电平变化标成"边沿事件"（哪路变了、上升还是下降、发生时刻），打成 `QEI_EdgeInfo` 结构：

[catchingEdge — qei.hpp:154-208](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/qei.hpp#L154-L208)

```cpp
edgeInfo.edges[0] = va_pre ^ va;  // A 是否变化
edgeInfo.types[0] = va;           // A 当前电平（1=上升沿, 0=下降沿）
edgeInfo.timeStep = timeStep;     // 发生时刻
```

**计数与测速 `calcCounter`**——内核最复杂的一段。它做三件事：4× 模式位置计数、转向判定、转速换算。转速换算片段：

[calcCounter 测速片段 — qei.hpp:421-441](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/qei.hpp#L421-L441)

```cpp
processABedges(edges_pre, edges_cur, mode, dir, ii_cycles);  // 判方向 + 算间隔
ap_uint<36> tmp_rpm = freq;      // 100MHz
tmp_rpm *= 15;                   // ×60/4（4× 解码）
unsigned int div = ii_cycles * cpr;
tmp_rpm = tmp_rpm / div;         // RPM
...
tmp.range(15, 0)  = speed_rpm;            // 低16=转速
tmp.range(31, 16) = counter >> 2;         // 高16=角度（4×计数除回1×）
axi_qei_rpm_theta_m.write((int)tmp);      // 与 FOC 输入格式一致
```

注意 `tmp` 的打包格式与 FOC 的 `FOC_RPM_THETA_m` 完全对齐（低 16 位 RPM、高 16 位角度）——**QEI 的输出可以直接喂给 FOC 的输入**，这就是这两个 IP 在系统里串联的依据。

另外，`calcCounter` 还有一个**超时保护**：若长时间（`QEI_MAX_NO_EDGE_CYCLE`）没有任何边沿，判定为编码器掉线，输出错误码 `err=3` 并复位计数器。

#### 4.3.4 代码实践：阅读型 + 参数追踪

**实践目标**：验证 QEI 输出格式与 FOC 输入格式一致，理解 4× 解码的含义。

**操作步骤**：

1. 打开 `qei.hpp` 的 `calcCounter`（L310），找到上面那段 `tmp.range(15,0)` / `tmp.range(31,16)` 打包代码。
2. 打开 `foc.hpp` 的 `hls_foc_strm_ap_fixed`（L1084-1085），对比 `RPM_in = (in & 0xFFFF)` / `Angle_in = (in >> 16)` 的解包顺序。
3. 在 `qei.hpp` 中找 `counter < ((int)(cpr << 2) - 1)`（L412）这句，确认 `cpr << 2` 即 4× 模式：每个 A/B 边沿（上升+下降，两路共 4 个/周期）都计数，分辨率提高 4 倍。

**需要观察的现象**：QEI 把角度放在高 16 位、转速放低 16 位；FOC 恰好按相同顺序解包。`counter >> 2` 把 4× 计数除回每转 CPR 的量纲。

**预期结果**：你能用一句话说明"QEI 的 `RPM_THETA_m` 流可以直连 FOC 的 `FOC_RPM_THETA_m` 流"，并解释 4× 解码为何把 `cpr` 乘 4。

**待本地验证**：csim 跑 `motor_control/L1/tests/IP_QEI`（`topfunction = hls_qei`）确认功能。

#### 4.3.5 小练习与答案

**练习 1**：QEI 的 `filterIn` 滤波窗口是 16，意味着什么？
**参考答案**：输入信号必须连续 16 个时钟周期保持同一电平，滤波输出才会更新；任何短于 16 拍的毛刺都被忽略。100MHz 时钟下相当于滤除 160ns 以内的干扰。

**练习 2**：为什么 QEI 用 4× 解码（`cpr << 2`），最后又 `counter >> 2` 还原？
**参考答案**：A、B 两路各有上升、下降两种边沿，一圈共 4×CPR 个边沿；利用全部边沿（4×）能把位置分辨率提高到原来的 4 倍，低速时定位更精细。输出角度时除以 4（`>>2`）还原成以 CPR 为量纲的角度，便于和 FOC 的角度约定对齐。

---

### 4.4 AXI 接口约定、IPI 集成与 FP32 模型分支

#### 4.4.1 概念说明

前三个模块讲的是"算什么"，这一模块讲"怎么接进系统"。motor_control 的 4 个内核有**高度统一的接口约定**，这个约定是为 **IPI（IP integrator）图形化集成**量身定制的：

- **数据流**用 **AXI4-Stream**（`axis`）：电流、电压命令、占空比、门控脉冲、编码器脉冲等"流式"数据。
- **配置参数**用 **AXI4-Lite**（`s_axilite`）：PID 参数、模式、设定点等运行时由处理器改写的量，全部归入一个名为 `pwm_args` 的 bundle，每个参数有固定 `offset`。
- **状态回读**用 **AXI4-Lite + `ap_none`**：内核内部的中间量（各 PID 的误差、累积、输出，当前的占空比、PWM 周期等）通过 `hls::ap_none<int>` 端口持续驱动到 AXI-Lite 寄存器，处理器可随时读取观测。

这种"流进流出 + 寄存器配置 + 寄存器观测"的三段式，让每个内核综合后成为一个**带 AXI 接口的独立 IP**，可以在 Vivado 的 IP integrator 里像搭积木一样摆放、连线，构成完整的电机驱动系统（FOC → SVPWM_DUTY → PWM_GEN，外加 QEI 反馈）。

**关于 FP32 模型分支**：2026.1 版本有一个重要变化（见 README）——默认的 `ap_fixed` 定点 HLS 源码**回退到了 2024.1 的实现**（为 KD240 应用集成做准备），而新近引入的 **FP32 浮点模型与精度测试基础设施**保留在 `L1/include/models_fp` 和 `L1/tests/tests_fp32` 下。`models_fp` 不是用来替代默认实现的，而是提供**精度参考**：用纯浮点 `float` 实现同样的 Clarke/Park/PI/SVPWM，作为黄金参考，用来量化定点实现引入的数值误差。

#### 4.4.2 核心流程

**DUT 封装的标准套路**（以 SVPWM 前端为例）：

```
   xf::motorcontrol::hls_svpwm_duty_axi<...>(...)   ← 库提供的模板内核（无接口绑定）
                          │
                          ▼ 包一层
   void hls_svpwm_duty(...) {                        ← 测试目录里的 DUT 顶层（extern）
       #pragma HLS interface axis  port = strm_Va_cmd   ← 数据流绑 AXI-Stream
       #pragma HLS interface s_axilite port = pwm_args_dc_link_ref offset=0x10 bundle=pwm_args
       ...
       #pragma HLS interface s_axilite port = return bundle = pwm_args  ← 控制握手也走 AXI-Lite
       #pragma HLS stable variable = pwm_args_dc_link_ref              ← 声明参数运行中可变但非每次读
       xf::motorcontrol::hls_svpwm_duty_axi<...>(...);  ← 调用模板
   }
```

**FP32 vs 默认 `ap_fixed` 的差异**（以 SVPWM 为例）：

| 维度 | 默认 `ap_fixed`（`hw/svpwm.hpp`） | FP32（`models_fp/svpwm_fp32.hpp`） |
|---|---|---|
| 数据类型 | `ap_fixed<24,8>` / `ap_ufixed<16,0>` | `float` |
| 命名空间 | `xf::motorcontrol::details` | `xf::motorcontrol::hls`（可综合）/ `golden`（模板参考） |
| 接口 | AXI Stream + AXI-Lite 完整 IP | 纯 C++ 函数（`inline`，含 `PIPELINE II=1`） |
| 除以 2 | `>> 1`（移位） | `/ 2.0f`（浮点除法） |
| 用途 | 实际上板驱动电机 | 精度参考、资源/精度权衡研究 |

#### 4.4.3 源码精读

**SVPWM 前端的 DUT 封装**——注意三类接口绑定的写法：

[hls_svpwm_duty DUT 封装 — ip_svpwm.cpp:31-83](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/tests/IP_SVPWM/src/ip_svpwm.cpp#L31-L83)

关键几行：

```cpp
void hls_svpwm_duty(...) {
#pragma HLS interface axis port = strm_Va_cmd            // ① 数据流 → AXI-Stream
...
#pragma HLS interface s_axilite port = pwm_args_dc_link_ref offset = 0x10 bundle = pwm_args  // ② 配置 → AXI-Lite
#pragma HLS interface s_axilite port = pwm_stt_cnt_iter  offset = 0x18 bundle = pwm_args      // ③ 状态 → AXI-Lite
#pragma HLS interface ap_ctrl_hs port = return
#pragma HLS interface s_axilite port = return bundle = pwm_args
#pragma HLS stable variable = pwm_args_dc_link_ref       // 声明：参数运行时可变
    long pwm_args_cnt_trip = 0x7fffffffffffffffL;        // 仿真/上板：近乎无限循环
    xf::motorcontrol::hls_svpwm_duty_axi<t_svpwm_cmd, t_svpwm_ratio>(...);  // 调模板内核
}
```

三个要点：

1. **`bundle = pwm_args`**：所有 `s_axilite` 端口归到同一个 AXI-Lite 从端寄存器组，每个有固定 `offset`（0x10、0x18、0x28…），处理器按地址读写。
2. **`ap_none` 端口**（库内核里的 `hls::ap_none<int>& pwm_stt_cnt_iter`）：HLS 会把它综合成一个由内核持续更新的寄存器，处理器读它得到"当前已发出多少条 PWM 命令"——这是**可观测性**的来源。
3. **`stable` 指令**：告诉 HLS "这个变量在函数执行期间可能被外部改写，但不需要每次访问都重新读寄存器"，让综合器在"每次读"和"缓存一次"之间做优化。
4. **`pwm_args_cnt_trip = 0x7fffffffffffffffL`**：上板时前端跑近乎无限循环（真正的"一直在算"），仅 `#ifdef SIM_FINITE`（仿真）时才用有限次数 `TESTNUMBER=10` 提前结束。

**FOC 的 DUT 封装同理**，只是参数更多（PID 参数几十个），全部进 `s_axilite` bundle：

[hls_foc_periodic_ap_fixed DUT 封装 — ip_foc.cpp:32-90](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/tests/IP_FOC/src/ip_foc.cpp#L32-L90)

**FP32 SVPWM 参考模型**——对比默认实现的鞍形算法，注意它额外输出了 `sector`（扇区号，用 `atan2` 算）和 `voff` 等中间量，方便调试：

[SVPWM_fp32 — svpwm_fp32.hpp:105-127](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/models_fp/svpwm_fp32.hpp#L105-L127)

```cpp
inline void SVPWM_fp32(SVPWMOutput_fp32& output, float va_cmd, float vb_cmd, float vc_cmd, float dc_link) {
#pragma HLS INLINE
#pragma HLS PIPELINE II = 1
    float voff = get_voff_fp32(va_cmd, vb_cmd, vc_cmd);
    float va_saddle = va_cmd - voff;
    ...
    float duty_a = get_duty_ratio_fp32(va_saddle, dc_link);
    ...
}
```

`models_fp` 的 README 说明得很清楚：每个算子有**两个版本**——`*_fp32.hpp`（命名空间 `hls`，含 pragma、可综合、用于量资源）和无后缀的 `*.hpp`（命名空间 `golden`，是 `template<T>` 模板、无 pragma、用 `double` 实例化时是最高精度参考）。`tests_fp32` 下的测试把 `_fp32` 可综合版的输出与 `double` 黄金参考对比，量化浮点舍入误差；也可以用 `ap_fixed` 实例化黄金模板，量化定点量化误差。详见 [models_fp/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/models_fp/README.md)。

#### 4.4.4 代码实践：对比 FP32 与默认实现

**实践目标**：亲手对比 `models_fp/svpwm_fp32.hpp`（浮点）与 `hw/svpwm.hpp`（定点）两套 SVPWM 实现，理解精度差异的来源。

**操作步骤**：

1. 并排打开 [svpwm.hpp 的 GetVoff/GetRatio/calculate_ratios_core](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/svpwm.hpp#L114-L167) 与 [svpwm_fp32.hpp 的 get_voff_fp32/get_duty_ratio_fp32/SVPWM_fp32](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/models_fp/svpwm_fp32.hpp#L60-L127)。
2. 列出数学上等价、但实现不同的地方：
   - 定点版 `(Vmin + Vmax) >> 1` vs 浮点版 `(vmin + vmax) / 2.0f`；
   - 定点版 `ratio = (Vp_saddle >> 1) / MAXVAL` vs 浮点版 `(vp_saddle / 2.0f) / max_val`。
3. 进入 `motor_control/L1/tests/tests_fp32/svpwm_apfixed_tb`，读 `src/svpwm_apfixed_top.cpp`：它用一个非模板顶层包装 `details::calculate_ratios_core<ap_fixed<32,16>>`，正是为了把定点核心单独拿出来综合与对比。
4. 在该目录跑 `make run TARGET=csim`（若环境就绪），观察测试如何比对定点输出与浮点参考、给出误差。

**需要观察的现象**：定点版用 `>>1`（整数移位）代替 `/2`，用定点除法代替浮点除法；这些是误差的来源。FP32 版用 `float` 运算，更准但综合后占更多 DSP。

**预期结果**：你能指出两处"移位 vs 除法"的差异，并说明 `tests_fp32/svpwm_apfixed_tb` 存在的意义——把定点 `calculate_ratios_core` 与浮点参考对比，量化定点误差。

**待本地验证**：csim 的实际误差数值需本地运行得到。

#### 4.4.5 小练习与答案

**练习 1**：为什么所有内核都把配置参数归到一个 `bundle = pwm_args` 的 AXI-Lite 组，而不是每个参数一个独立端口？
**参考答案**：归到一个 AXI-Lite 从端后，整个 IP 只暴露一个 AXI-Lite 接口给处理器（占用一个 AXI 地址段），处理器按 `offset` 读写各参数；若每个参数独立端口，会爆炸式增加接口数量，不利于 IPI 里连线和地址分配。

**练习 2**：`models_fp` 里的 `*_fp32.hpp` 和无后缀的 `*.hpp` 都叫"参考模型"，它们的区别是什么？
**参考答案**：`*_fp32.hpp`（命名空间 `hls`）硬编码 `float`、含 HLS pragma、可被 Vitis HLS 综合，用于在 FPGA 上量浮点实现的资源/时序；无后缀的 `*.hpp`（命名空间 `golden`）是 `template<typename T>` 模板、无 pragma、纯 C++，用 `double` 实例化时是最高精度黄金参考，用 `ap_fixed` 实例化时可量化定点误差。前者是"可综合的浮点实现"，后者是"任意类型的精度标尺"。

---

## 5. 综合实践

**任务**：把本讲的 4 个模块串起来，画出一条完整的电机控制信号链，并回答两个核心问题。

**步骤**：

1. **画信号链**：在一张图上标出 `QEI → FOC → SVPWM_DUTY → PWM_GEN` 的数据流向，标注每一段用的是哪种接口（AXI Stream 还是 AXI-Lite），以及数据格式（如 `RPM_THETA_m` 的低/高 16 位、`Va_cmd` 的 `ap_fixed<24,8>`、占空比的 `ap_ufixed<16,0>`、门控的 `ap_uint<1>`）。

2. **回答问题 A（前后端配合）**：阅读 [test_svpwm.cpp 的 SVPWM_wrapper（L70-120）](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/tests/IP_SVPWM/src/test_svpwm.cpp#L70-L120)，用 3-5 句话说明 **SVPWM_DUTY 与 PWM_GEN 如何配合完成一次空间矢量调制**。要点包括：前端算占空比、后端生成门控、二者靠 `strm_duty_cycle_{a,b,c}` 流解耦、整体在 DATAFLOW 区域并发、后端速率高于前端。

3. **回答问题 B（FP32 差异）**：对比 [hw/svpwm.hpp 的 calculate_ratios_core](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/hw/svpwm.hpp#L141-L167) 与 [models_fp/svpwm_fp32.hpp 的 SVPWM_fp32](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/motor_control/L1/include/models_fp/svpwm_fp32.hpp#L105-L127)，指出 **`models_fp` 相对默认 `ap_fixed` 实现的三点差异**：数据类型（`float` vs `ap_fixed`）、运算方式（浮点除法 vs 移位+定点除法）、用途（精度参考 vs 实际驱动）。

4. **延伸（可选）**：在图上标出"弱磁控制"插入的位置（FOC 内部，`Field_Weakening_T` 调整 `Vd`），以及"ADC 同步采样"由哪个信号触发（PWM_GEN 的 `strm_sync_*`）。

**预期产出**：一张信号链图 + 两段文字说明。完成后你应当能用一段话向别人讲清"从编码器脉冲进，到 6 路门控脉冲出"的完整通路。

---

## 6. 本讲小结

- motor_control 是一个**纯 PL（HLS）路线**的垂直库，提供 **FOC / SVPWM_DUTY / PWM_GEN / QEI** 四个算法级 L1 API，覆盖"测位置→算控制律→调电压→生成门控"的完整电机驱动链路。
- **FOC** 是大脑：Clarke/Park 变换把三相电流解耦到 `dq` 轴，三路 PID（速度/磁通/力矩）+ 解耦 + 弱磁做闭环，再反变换回三相电压命令；因控制律重而用 `II=5`。
- **SVPWM_DUTY（前端）** 用 min-max 零序注入法把三相电压命令换算成 0~1 占空比；**PWM_GEN（后端）** 根据占空比生成 6 路带死区的门控脉冲 + ADC 同步信号；二者速率不同，靠占空比流在 DATAFLOW 区域解耦并发。
- **QEI** 经"滤波→捕边→计数"三级，从编码器 A/B/I 脉冲还原出转速+角度，输出格式 `RPM_THETA_m` 与 FOC 输入完全对齐，可直接串联。
- 四个内核遵循统一的三段式接口：**AXI Stream（数据）+ AXI-Lite（配置 `*_args`）+ AXI-Lite `ap_none`（状态 `*_stts`）**，是为 Vivado **IPI 图形化集成**设计的；每个综合成独立 IP，按地址 `offset` 读写参数。
- **`models_fp`** 是新增的 FP32 浮点参考模型分支（与 2024.1 定点源码并存），提供 `*_fp32`（可综合）与模板黄金参考两套，用于量化定点实现的精度误差，**不替代**默认实现。

---

## 7. 下一步学习建议

- **若关心 HLS 接口综合**：回到 u3-l1/u3-l2，对照本讲看到的大量 `#pragma HLS interface axis/s_axilite/ap_ctrl_hs` 与 `stable`/`ap_none` 指令，深化对"AXI 接口如何从 C++ 函数签名综合出来"的理解。
- **若关心系统级集成**：本讲只到 L1 内核级。后续可关注这些 IP 如何在 Vivado IP integrator（IPI）里连成完整系统、如何打包成 xclbin/Bitstream——可参考 u5-l1（v++ 构建流程）与 u15-l2（完整部署）。
- **若关心精度权衡**：深入 `motor_control/L1/tests/tests_fp32/` 下的测试台，看它们如何用 `double` 黄金参考量化 `ap_fixed` 的量化误差；这套"可综合浮点 + 模板黄金参考"的双模型方法论，与 solver 库（u7）里"PL HLS + AIE 两套实现互相验证"的思路异曲同工。
- **若关心其它垂直库**：下一讲 u11-l2 将讲 `ultrasound` 库，它同样是从 L1 向量运算组合出 L2/L3 应用，可对比两者的"由原语组合成系统"的不同风格。
