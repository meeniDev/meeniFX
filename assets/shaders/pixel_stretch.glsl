#version 330
// SAMPLING
uniform vec2 direction;
uniform float position, compression;
uniform int edge;
out vec4 result;
void main() {
    vec2 p = gl_FragCoord.xy-.5;
    float q = dot(p-(iResolution.xy-1.)*.5,direction);
    result = samplePixel(iChannel0,p+direction*(position-q)*(1.-compression),edge,false);
}
