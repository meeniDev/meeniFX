#version 330
// SAMPLING
uniform vec2 center;
uniform float radius, amount, wavelength, falloff, phase;
uniform int edge;
out vec4 result;
void main() {
    vec2 p = gl_FragCoord.xy-.5;
    vec2 delta = p-center;
    float r = length(delta);
    float inner = clamp(r/(wavelength*.5),0.,1.);
    inner = inner*inner*(3.-2.*inner);
    float outer = pow(max(0.,1.-r/radius),falloff);
    float offset = sin(r/wavelength*6.28318530718-phase)*amount*inner*outer;
    result = samplePixel(iChannel0,p+delta/max(r,1e-8)*offset,edge,false);
}
