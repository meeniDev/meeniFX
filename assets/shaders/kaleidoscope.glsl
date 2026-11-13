#version 330
// SAMPLING
uniform vec2 center;
uniform float zoom, rotation;
uniform int segments;
uniform bool mirrored;
out vec4 result;
void main() {
    vec2 delta = gl_FragCoord.xy-.5-center;
    float distance = max(1e-3,length(delta))/zoom;
    float wedge = 6.28318530718/float(segments);
    float theta = atan(delta.y,delta.x)+rotation;
    float folded = mod(theta,wedge);
    if (mirrored && segments%2==0) folded = abs(folded-wedge*.5);
    result = samplePixel(iChannel0,center+distance*vec2(cos(folded),sin(folded)),1,false);
}
