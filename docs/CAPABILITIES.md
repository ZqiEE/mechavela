# Evidence and scope

This snapshot derives from the locally tested RLSOK 0.3.2-dev.24 workbench, now branded **Mechavela 0.3.2-alpha.1**. Original installations and project history are preserved. Rebranding does not re-certify every earlier feature.

| Capability | Evidence / boundary |
|---|---|
| Local project persistence | Existing project/restart tests; rebranded source smoke results in this release's validation report |
| User AI project code/tools | Existing official Codex development runs; fresh-login and second-machine end-to-end acceptance not completed for this release |
| Real CAD and 3D | Actual FreeCAD STEP/FCStd, OCCT assembly measurements and colored meshes in the development environment; external host setup required |
| Mechanical-spider knee repair | 465 solids, 44 groups; 352 single-joint +25 additional configurations and 41 recorded cycle configurations checked |
| Native dynamics | Official MuJoCo 4.0.0 WASM, 10,000 integration steps /10s; fixed base, ideal drives, no ground contact |
| Local knee clearance bound | 0.499mm conservative nominal bound for each lower-link/own-upper-link pair; not the entire multi-joint space |
| Ground operation | Not accepted: 10 of41 recorded foot-mesh positions below Z=0, minimum about −17.3mm |
| Manufacturing/hardware | Strength, fatigue, actuator ratings, real assembly and hardware are not accepted |

The original project/run archives are private and not uploaded. These published numbers summarize recorded development evidence; public users cannot reproduce that private project from this source archive alone. They can author their own projects once dependencies are configured.
