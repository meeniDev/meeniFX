#version 330
// SAMPLING
uniform float amount, wavelength, detail, heightBias, phase, seedPhase;
uniform vec2 direction;
uniform int edge;
out vec4 result;
void main() {
    vec2 p = gl_FragCoord.xy-.5;
    float s = dot(p,direction)/wavelength;
    float t = dot(p,vec2(-direction.y,direction.x))/wavelength;
    float wave = sin(s*6.28318530718+phase) + detail*.65*sin(t*3.1-phase*2.+sin(s*1.7+seedPhase));
    float mask = pow(max(p.y/max(1.,iResolution.y-1.),1e-8),heightBias);
    result = samplePixel(iChannel0,p+direction*wave*amount*mask,edge,false);
}
