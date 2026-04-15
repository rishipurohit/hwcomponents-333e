# Milestone 2 Writeup

This week, we focused on modeling the 333-eDRAM cache in Accelforge. Concretely, this consisted of a few key steps:

1. Debugging the existing `hwcomponents` model of 333-eDRAM to ensure DESTINY was able to accurately model the architecture. We played around with the parameters being passed to DESTINY to ensure consistency with the results of the paper. Specifically, we had to experiment with the `MonolithicStackCount` parameter to account for the monolithic stacking that is characteristic of 333-eDRAM (the three types of transistors are layered one on top of the other monolithically). We also reconfigured 333-eDRAM and baseline 3D eDRAM to be set up as caches (as opposed to RAMs) to be more practical in terms of our downstream intended use cases, where these memories are used as scratchpads (in Eyeriss) and global buffers.

2. Then, we benchmarked area, latency, and leakage power for both 333-eDRAM and baseline 3D eDRAM using the `hwcomponents` model. We found that 333-eDRAM had a smaller area, lower latency, and lower leakage power than baseline 3D eDRAM, as expected. The YAMLs are available in `baseline_test.yaml` and `edram333_test.yaml`. The exact results are below:

## Baseline 3D eDRAM

Total area of the design: 4.31e-06 m^2
Area breakdown per component:
	MainMemory: 0.00e+00 m^2
	GlobalBuffer: 7.17e-07 m^2
	InputScratchpad: 1.18e-06 m^2
	WeightScratchpad: 1.18e-06 m^2
	OutputScratchpad: 1.18e-06 m^2
	MAC: 6.49e-08 m^2
Total leakage power of the design: 6.29e-01 W
	MainMemory: 0.00e+00 W
	GlobalBuffer: 6.83e-02 W
	InputScratchpad: 1.87e-01 W
	WeightScratchpad: 1.87e-01 W
	OutputScratchpad: 1.87e-01 W
	MAC: 7.73e-04 W

## 333-eDRAM

Total area of the design: 2.51e-06 m^2
Area breakdown per component:
	MainMemory: 0.00e+00 m^2
	GlobalBuffer: 4.29e-07 m^2
	InputScratchpad: 6.72e-07 m^2
	WeightScratchpad: 6.72e-07 m^2
	OutputScratchpad: 6.72e-07 m^2
	MAC: 6.49e-08 m^2
Total leakage power of the design: 8.59e-02 W
	MainMemory: 0.00e+00 W
	GlobalBuffer: 1.77e-02 W
	InputScratchpad: 2.25e-02 W
	WeightScratchpad: 2.25e-02 W
	OutputScratchpad: 2.25e-02 W
	MAC: 7.73e-04 W

3. Then, we moved on to creating a baseline configuration integrating our new 333-eDRAM component model in Accelforge, using the Eyeriss architecture as a reference (the tutorial was provided in the Accelforge repo). We replaced the use of SRAMs in the original Eyeriss architecture with our 333-eDRAM component model (and also a baseline 3D eDRAM model) to create two new configurations: `eyeriss_333.yaml` and `eyeriss_baseline.yaml`.

4. Finally, we ran the GPT workload on both configurations to evaluate the performance of 333-eDRAM in a realistic setting. The results are below:

## 333-eDRAM

Energy: 19.592543788727394J, 8.900829267762293e-12J/compute
	MainMemory: 4.477980518589654e-13J/compute
	GlobalBuffer: 1.5400778752211565e-12J/compute
	InputScratchpad: 1.9691706511886967e-12J/compute
	WeightScratchpad: 2.0477747432338413e-12J/compute
	OutputScratchpad: 2.1694670368000173e-12J/compute
	MAC: 7.265409094596154e-13J/compute
Latency: 173.16183958761394s, 7.866686330923093e-11s/compute
	MainMemory: 1.6681777270308173e-11s/compute
	MAC: 7.86668646890476e-11s/compute
	InputScratchpad: 8.841558263522967e-14s/compute
	OutputScratchpad: 1.9551324263993725e-13s/compute
	GlobalBuffer: 1.962920248929132e-12s/compute
	WeightScratchpad: 1.3030410626370392e-13s/compute

## Baseline 3D eDRAM

Energy: 115.0236974815312J, 5.225489339051963e-11J/compute
	MainMemory: 4.477980518589654e-13J/compute
	GlobalBuffer: 5.652830975951254e-12J/compute
	InputScratchpad: 1.4996972029169477e-11J/compute
	WeightScratchpad: 1.512007990862253e-11J/compute
	OutputScratchpad: 1.5310671515457795e-11J/compute
	MAC: 7.265409094596154e-13J/compute
Latency: 173.16183958761394s, 7.866686330923093e-11s/compute
	MainMemory: 1.6681777270308173e-11s/compute
	MAC: 7.86668646890476e-11s/compute
	InputScratchpad: 1.0916945515984655e-13s/compute
	OutputScratchpad: 2.1049985756207978e-13s/compute
	GlobalBuffer: 1.962920248929132e-12s/compute
	WeightScratchpad: 1.473148317741868e-13s/compute

All of our code for steps 2 through 4 is available in `component_energy_area.ipynb`.