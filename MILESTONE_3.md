# Milestone 3 Writeup

This week, we ran the three experiments specified in our milestone proposal.

The first step was to figure out the gain in memory capacity associated with swapping SRAMs with 333-eDRAM in the TPU v4 architecture. We ran three experiments, varying the configuration of the TPU as follows:

- Global Buffer only: Swapped SRAMs in both the global buffer and the local buffer for 333-eDRAM
- Local Buffer only: Swapped SRAMs only in the local buffer for 333-eDRAM
- Both: Swapped SRAMs in both the global buffer and the local buffer for 333-eDRAM

We actually switched our simulation for the 333 eDRAM on the advice of David Kong, who was involved in the 333-eDRAM project. He recommended that we model the 333-eDRAM with the MemSysExplorer software.

It's important to note that MemSysExplorer is actually built on top of DESTINY, so this is less of a big change as it might seem. Initially our approach was a bit hacky with DESTINY to model 333-eDRAM, but with MemSysExplorer we were able to model it more accurately going to the numbers used by the authors of the paper.

## Experiment 1: Testing Global vs Local vs Both

Note: we haven't made graphs for any of the experiments yet.

```

tpu_333_both.yaml
Energy: 1.3389697359711723J, 4.983108484061299e-09J/compute
	MainMemory: 5.644403835307388e-11J/compute
	GlobalBuffer: 4.374426154640787e-09J/compute
	LocalBuffer: 5.521539917870097e-10J/compute
	ScalarUnit: 0.0J/compute
	Register: 0.0J/compute
	MAC: 8.391676956143961e-14J/compute
Latency: 0.11137716116466567s, 4.145011527511561e-10s/compute
	MainMemory: 1.63457329180207e-12s/compute
	ScalarUnit: 1.8430776246554717e-15s/compute
	MAC: 1.451778049177139e-14s/compute
	LocalBuffer: 4.1450112786556303e-10s/compute
	Register: 0.0s/compute

tpu_333_global.yaml
Energy: 0.019836868219964512J, 7.382487207070177e-11J/compute
	MainMemory: 5.643289340101523e-11J/compute
	GlobalBuffer: 1.724731815699876e-11J/compute
	LocalBuffer: 6.074309842837761e-14J/compute
	ScalarUnit: 0.0J/compute
	Register: 0.0J/compute
	MAC: 8.391676956143961e-14J/compute
Latency: 0.0004391335009303887s, 1.6342788798553441e-12s/compute
	MainMemory: 1.6342505248153212e-12s/compute
	ScalarUnit: 1.8430776246554717e-15s/compute
	MAC: 1.451778049177139e-14s/compute
	LocalBuffer: 0.0s/compute
	Register: 0.0s/compute

tpu_333_local.yaml
Energy: 0.16350650169472203J, 6.085056556945723e-10J/compute
	GlobalBuffer: 8.004268227618481e-14J/compute
	MainMemory: 5.6187704455724755e-11J/compute
	LocalBuffer: 5.521539917870097e-10J/compute
	ScalarUnit: 0.0J/compute
	Register: 0.0J/compute
	MAC: 8.391676956143961e-14J/compute
Latency: 0.11137716116466567s, 4.145011527511561e-10s/compute
	GlobalBuffer: 2.9251766143329437e-15s/compute
	MainMemory: 1.6271500206249678e-12s/compute
	ScalarUnit: 1.8430776246554717e-15s/compute
	MAC: 1.451778049177139e-14s/compute
	LocalBuffer: 4.1450112786556303e-10s/compute
	Register: 0.0s/compute
Generating pmappings: 100%|██████████| 650/650 [00:13<00:00, 47.80it/s] 
--------------------------------
tpu_control.yaml
Energy: 0.015156846297087998J, 5.640770610204112e-11J/compute
	GlobalBuffer: 7.519885367601104e-14J/compute
	MainMemory: 5.6187704455724755e-11J/compute
	LocalBuffer: 6.088602307891648e-14J/compute
	ScalarUnit: 0.0J/compute
	Register: 0.0J/compute
	MAC: 8.391676956143961e-14J/compute
Latency: 0.0004377065525149604s, 1.6289683377593481e-12s/compute
	GlobalBuffer: 2.6274487685377934e-15s/compute
	MainMemory: 1.6271500206249678e-12s/compute
	ScalarUnit: 1.8430776246554717e-15s/compute
	MAC: 1.451778049177139e-14s/compute
	LocalBuffer: 0.0s/compute
	Register: 0.0s/compute
```

## Experiment 2: Increasing Sequence Length

Qualitatively, we see the expected scaling with sequence length. As sequence length increases, the amount of off-chip memory traffic increases to fetch the input and weights. We also see that the 333-eDRAM configuration scales better than the baseline, with 333-eDRAM improving as sequence length increases (as expected, since 333-eDRAM has better area, latency, and leakage power).